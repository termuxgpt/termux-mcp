import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from .config import AUTO_INPUT_INTERVAL, COMMAND_TIMEOUT, HOME
from .utils import is_install_command

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

# Per-thread state — fixes Bug 7 (global state race conditions)
_thread_local = threading.local()


def _get_tld() -> threading.local:
    if not hasattr(_thread_local, 'current_dir'):
        _thread_local.current_dir = os.getcwd()
        _thread_local.active_pid = None
        _thread_local.pid_lock = threading.Lock()
    return _thread_local


def get_current_dir() -> str:
    return _get_tld().current_dir


def set_current_dir(path: str) -> None:
    _get_tld().current_dir = path


def get_active_pid() -> Optional[int]:
    tld = _get_tld()
    with tld.pid_lock:
        return tld.active_pid


def cancel_active() -> bool:
    tld = _get_tld()
    with tld.pid_lock:
        pid = tld.active_pid
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


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


def execute_streaming(handler: "BaseHTTPRequestHandler", raw_cmd: str) -> None:
    raw_cmd = raw_cmd.strip()

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
                    _run_process(handler, chained)
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

    _run_process(handler, raw_cmd)


def _run_process(handler: "BaseHTTPRequestHandler", raw_cmd: str) -> None:
    tld = _get_tld()
    cmd = preprocess(raw_cmd)
    process = None
    killed = threading.Event()

    try:
        popen_kwargs = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.PIPE,
            "text": True,
            "cwd": tld.current_dir,
        }
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid

        process = subprocess.Popen(f"export PAGER=cat; {cmd}", **popen_kwargs)

        with tld.pid_lock:
            tld.active_pid = process.pid

        _spawn_auto_input(process, raw_cmd)

        # Bug 6 fix: enforce timeout during streaming using a watchdog thread
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
            _send_chunk(handler, line)
            if killed.is_set():
                _send_chunk(handler, f"\n⏱️ Timed out after {COMMAND_TIMEOUT}s\n")
                break

        watchdog.join(timeout=2)

        if not killed.is_set():
            if process.returncode and process.returncode != 0:
                _send_chunk(handler, f"\n❌ Exit code: {process.returncode}\n")
            else:
                _send_chunk(handler, "\n✅ Done\n")

    except Exception as e:
        _send_chunk(handler, f"\n❌ Error: {e}\n")
    finally:
        with tld.pid_lock:
            tld.active_pid = None
        _finalize_chunks(handler)
