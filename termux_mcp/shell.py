import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from .config import AUTO_INPUT_INTERVAL, COMMAND_TIMEOUT, HOME, MAX_OUTPUT_BYTES
from .security import get_risk_assessment
from .utils import is_install_command, json_response, kill_process_group

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

# ── Working directory ─────────────────────────────────────────────────────
#
# Process-wide, NOT thread-local.
#
# This was moved into threading.local() to remove a race. But
# ThreadingHTTPServer handles every request on a *new* thread and reuses
# none, so `cd` in one request was invisible to the next — the README's
# "maintains persistent cd state across requests" was untrue for the only
# transport the REST API has. Every get_current_dir() call returned the
# daemon's startup directory.
#
# The consequence to be aware of: over HTTP the working directory is shared
# by every client, so one caller's `cd` affects the next. That is now stated
# in the README rather than contradicted by it. WebSocket connections keep
# their own cwd in websocket.py and do not come through here.
#
# The race the thread-local was meant to fix is real, so this is lock-guarded.
_cwd_lock = threading.Lock()
_current_dir: str = os.getcwd()


def get_current_dir() -> str:
    with _cwd_lock:
        return _current_dir


def set_current_dir(path: str) -> None:
    global _current_dir
    with _cwd_lock:
        _current_dir = path


# ── Cancellation registry (process-wide, deliberately NOT thread-local) ────
#
# `threading.local()` above cannot serve cancellation. ThreadingHTTPServer
# handles every request on its own thread, so a `/cancel` request read
# `active_pid` from a thread that had never run a command, got `None`, and
# killed nothing — `/cancel` could never work over HTTP, which is the only
# transport the REST API has.
#
# So the running pids live here, shared across threads. The per-thread
# `active_pid` above is kept as-is for `/env`'s `active_command_pid` report.
_active_pids: set = set()
_active_pids_lock = threading.Lock()

# How long a stopped command gets to exit on SIGTERM before SIGKILL.
CANCEL_GRACE = 0.4


def register_active_pid(pid: int) -> None:
    with _active_pids_lock:
        _active_pids.add(pid)


def unregister_active_pid(pid: int) -> None:
    with _active_pids_lock:
        _active_pids.discard(pid)


def get_active_pid() -> Optional[int]:
    """The pid of a command currently running, from the shared registry.

    This previously read a thread-local that only the *running* request's
    thread had ever written. Since each HTTP request gets its own fresh
    thread, it was always None — /env reported active_command_pid: null
    unconditionally, whatever was running.
    """
    with _active_pids_lock:
        return next(iter(_active_pids), None)


def active_pids() -> list:
    with _active_pids_lock:
        return sorted(_active_pids)


def cancel_active(pid: Optional[int] = None) -> bool:
    """Stop a running command. True if anything was signalled.

    Pass `pid` to stop one specific command. Without it, *every* running
    command is signalled — which is why the REST handler requires either a pid
    or an explicit `all: true`: HTTP carries no session identity, so an
    unscoped cancel lets any client kill whatever any other client is running.

    A pid that is not in the registry is refused rather than signalled. The
    registry only holds commands this daemon started, so without that check a
    caller could name any process on the device.

    Signals the **process group**, not just the pid: the daemon starts each
    command with `setsid`, so a command's pid is also its group id, and
    signalling the group reaches the command's children rather than orphaning
    them (an `sh -c 'ffmpeg ...'` would otherwise leave ffmpeg running).
    """
    if pid is not None:
        with _active_pids_lock:
            if pid not in _active_pids:
                return False
        targets = [pid]
    else:
        targets = active_pids()

    if not targets:
        return False

    def signal_group(target: int, sig: int) -> bool:
        try:
            if hasattr(os, "killpg"):
                os.killpg(target, sig)
            else:
                os.kill(target, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    killed = False
    for target in targets:
        if signal_group(target, signal.SIGTERM):
            killed = True

    # Give well-behaved commands a moment to exit, then force the rest. Bounded,
    # so /cancel cannot hang the client that asked for it.
    if killed:
        time.sleep(CANCEL_GRACE)
        # SIGKILL is Unix-only; falling back to SIGTERM keeps this importable
        # and callable on a platform that lacks it rather than raising inside
        # the cancellation path.
        force = getattr(signal, "SIGKILL", signal.SIGTERM)
        # Re-uses `targets`, not a fresh read of the registry. Re-reading here
        # picked up commands that *started during the grace window* and killed
        # those too — so cancelling one command could kill an unrelated one
        # that had only just begun.
        for target in targets:
            signal_group(target, force)

    return killed


def _inject_noninteractive(cmd: str) -> str:
    return f"export DEBIAN_FRONTEND=noninteractive; {cmd}"


def _inject_auto_yes(cmd: str) -> str:
    from .config import AUTO_YES_COMMANDS
    for trigger in AUTO_YES_COMMANDS:
        if trigger in cmd and "-y" not in cmd:
            cmd = cmd.replace(trigger, f"{trigger} -y")
    return cmd


def preprocess(cmd: str) -> str:
    cmd = _inject_auto_yes(cmd)
    cmd = _inject_noninteractive(cmd)
    return cmd


def handle_cd(raw_cmd: str) -> tuple:
    """Handle cd command — properly supports cd <path>; chained-command."""
    rest = raw_cmd[2:].strip()
    path_part = rest
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1:
            path_part = rest[:idx].strip()
            break

    if not path_part or path_part == "~":
        set_current_dir(HOME)
        return True, HOME

    raw_path = path_part.strip().replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path) else os.path.join(get_current_dir(), raw_path)
    )

    if os.path.isdir(new_path):
        set_current_dir(new_path)
        return True, get_current_dir()

    return False, f"Directory not found: {new_path}"


def _send_chunk(handler: "BaseHTTPRequestHandler", text: str) -> None:
    data = text.encode()
    size = hex(len(data))[2:].encode()
    try:
        handler.wfile.write(size + b"\r\n" + data + b"\r\n")
        handler.wfile.flush()
    except Exception:
        pass


def _finalize_chunks(handler: "BaseHTTPRequestHandler") -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
    except Exception:
        pass


def _feed_stdin(process: subprocess.Popen, data: str) -> None:
    """Write data to the child's stdin, then close it.

    Closing is what signals EOF. Without it a reader like `base64 -d` waits
    forever, and the command never finishes.

    Runs on a thread so a child that does not read stdin cannot block the
    request once the pipe buffer fills.
    """
    def _worker() -> None:
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def _spawn_auto_input(process: subprocess.Popen, cmd: str) -> None:
    """Only spawn auto-yes for package install commands (Bug 5 fix)."""
    if not is_install_command(cmd):
        return

    def _worker() -> None:
        try:
            while process.poll() is None:
                time.sleep(AUTO_INPUT_INTERVAL)
                try:
                    process.stdin.write("y\n")
                    process.stdin.flush()
                except Exception:
                    break
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def execute_streaming(handler: "BaseHTTPRequestHandler", raw_cmd: str,
                      stdin_data: Optional[str] = None) -> None:
    """Run a command, streaming its output to the client.

    `stdin_data` is written to the child's stdin and then closed. It exists so
    payloads do not have to travel in argv: the command is passed as a single
    element to `sh -c`, and Linux caps one argv element at MAX_ARG_STRLEN
    (128 KB). base64 inflates by 4/3, so any /write over ~96 KB of content
    died with "Argument list too long".
    """
    raw_cmd = raw_cmd.strip()

    # Every shell-backed endpoint funnels through here, so this is the one
    # place the check cannot be forgotten. It was previously called from just
    # two handlers — /run and the MCP run tool — leaving ~120 endpoints
    # ungated, including /write, /delete, /patch, /cron-add, /service-guard
    # and /ssh-wizard.
    #
    # This covers the DANGEROUS tier only. Those commands have no legitimate
    # use from any endpoint and no handler constructs one. The WARNING tier
    # is left where it is, on /run, which already knows how to hand the caller
    # the confirmation payload and accept `confirmed: true` on the retry —
    # adding that requirement to every endpoint in this change would turn
    # working calls into confirmation prompts.
    risk = get_risk_assessment(raw_cmd)
    if risk["blocked"]:
        json_response(handler, 403, {
            "error": risk["message"],
            "risk_level": risk["risk_level"],
            "blocked": True,
        })
        return

    if raw_cmd.startswith("cd"):
        ok, msg = handle_cd(raw_cmd)
        rest = raw_cmd[2:].strip()
        for sep in (";", "&&"):
            idx = rest.find(sep)
            if idx != -1:
                chained = rest[idx + len(sep):].strip()
                if chained and ok:
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/plain")
                    handler.send_header("Transfer-Encoding", "chunked")
                    handler.end_headers()
                    _run_process(handler, chained, stdin_data)
                    return
                break

        body = (msg + "\n").encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    _run_process(handler, raw_cmd, stdin_data)


def _run_process(handler: "BaseHTTPRequestHandler", raw_cmd: str,
                 stdin_data: Optional[str] = None) -> None:
    cmd = preprocess(raw_cmd)
    process = None
    killed = threading.Event()
    watchdog = None
    sent_bytes = 0
    truncated = False
    truncation_marker = (
        f"\n[Output truncated: max {MAX_OUTPUT_BYTES} bytes — "
        f"full output not sent]\n"
    )

    try:
        popen_kwargs = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.PIPE,
            "text": True,
            "cwd": get_current_dir(),
        }
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid

        process = subprocess.Popen(f"export PAGER=cat; {cmd}", **popen_kwargs)

        # The process-wide registry, which is what /cancel and /env read.
        register_active_pid(process.pid)

        if stdin_data is not None:
            # Caller-supplied payload takes the place of auto-input: the
            # command is reading stdin for its data, not sitting at a prompt.
            _feed_stdin(process, stdin_data)
        else:
            _spawn_auto_input(process, raw_cmd)

        # Timeout watchdog — only armed when TERMUX_MCP_TIMEOUT > 0.
        # Default 0 = commands run until they finish (pkg upgrade etc.).
        if COMMAND_TIMEOUT > 0:
            def _timeout_watchdog() -> None:
                try:
                    process.wait(timeout=COMMAND_TIMEOUT)
                except subprocess.TimeoutExpired:
                    killed.set()
                    try:
                        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                            time.sleep(1)
                        process.kill()
                    except Exception:
                        process.kill()

            watchdog = threading.Thread(target=_timeout_watchdog, daemon=True)
            watchdog.start()

        # Read stdout line by line (Bug 6: watchdog runs in parallel)
        for line in process.stdout:
            if killed.is_set():
                _send_chunk(handler, f"\n⏱️ Timed out after {COMMAND_TIMEOUT}s\n")
                break
            # Output cap: send up to MAX_OUTPUT_BYTES, then drain silently so
            # the process still finishes naturally (install prompts etc.).
            sent_bytes += len(line.encode())
            if sent_bytes <= MAX_OUTPUT_BYTES:
                _send_chunk(handler, line)
            elif not truncated:
                truncated = True
                _send_chunk(handler, truncation_marker)

        if watchdog is not None:
            watchdog.join(timeout=2)

        # Reap the child so returncode is actually populated before we read it.
        #
        # Draining stdout to EOF does NOT set returncode — only wait()/poll() do
        # — and nothing else calls either for an ordinary command: the watchdog
        # above arms only when COMMAND_TIMEOUT > 0 (default 0), and the
        # auto-input thread polls only for install commands. Without this,
        # returncode stayed None, `if process.returncode and ...` was falsy, and
        # EVERY command reported "✅ Done" — including `false`, `exit 7` and a
        # missing binary. Verified against a real shell.
        #
        # wait(), not poll(): immediately after EOF, poll() still loses the race
        # with the kernel reaping the child and returns None. Bounded, so a
        # command that closed stdout but lives on cannot hold the response open
        # — it just falls back to the old behaviour instead of hanging.
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

        if not killed.is_set():
            if process.returncode and process.returncode != 0:
                _send_chunk(handler, f"\n❌ Exit code: {process.returncode}\n")
            else:
                _send_chunk(handler, "\n✅ Done\n")

    except Exception as e:
        _send_chunk(handler, f"\n❌ Error: {e}\n")
    finally:
        if process is not None:
            # Reap the child. Previously nothing did, so a client that
            # disconnected mid-command — or any error in the loop above —
            # leaked a process that ran forever and could not be cancelled.
            kill_process_group(process)
            unregister_active_pid(process.pid)
        _finalize_chunks(handler)
