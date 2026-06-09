import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from .config import AUTO_INPUT_INTERVAL, AUTO_YES_COMMANDS, COMMAND_TIMEOUT, HOME

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

_current_dir: str = os.getcwd()
_active_pid: Optional[int] = None
_pid_lock = threading.Lock()


def get_current_dir() -> str:
    return _current_dir


def set_current_dir(path: str) -> None:
    global _current_dir
    _current_dir = path


def get_active_pid() -> Optional[int]:
    with _pid_lock:
        return _active_pid


def cancel_active() -> bool:
    with _pid_lock:
        pid = _active_pid
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
    for trigger in AUTO_YES_COMMANDS:
        if trigger in cmd and "-y" not in cmd:
            cmd = cmd.replace(trigger, f"{trigger} -y")
    return cmd


def preprocess(cmd: str) -> str:
    cmd = _inject_auto_yes(cmd)
    cmd = _inject_noninteractive(cmd)
    return cmd


def handle_cd(raw_cmd: str) -> tuple[bool, str]:
    """Handle cd command — properly supports cd <path>; chained-command."""
    # Strip "cd " prefix and extract the path (could be followed by ; or &&)
    rest = raw_cmd[2:].strip()
    # Stop at first ; or && to get just the path
    path_part = rest
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1:
            path_part = rest[:idx].strip()
            break

    if not path_part or path_part == "~":
        set_current_dir(HOME)
        return True, f"{_current_dir}"

    raw_path = path_part.strip().replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path) else os.path.join(_current_dir, raw_path)
    )

    if os.path.isdir(new_path):
        set_current_dir(new_path)
        return True, f"{_current_dir}"

    return False, f"Directory not found: {new_path}"


def _send_chunk(handler: "BaseHTTPRequestHandler", text: str) -> None:
    data = text.encode()
    size = hex(len(data))[2:].encode()
    handler.wfile.write(size + b"\r\n" + data + b"\r\n")
    handler.wfile.flush()


def _finalize_chunks(handler: "BaseHTTPRequestHandler") -> None:
    handler.wfile.write(b"0\r\n\r\n")


def _spawn_auto_input(process: subprocess.Popen) -> None:
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
        # If cd was followed by a chained command, execute the rest
        rest = raw_cmd[2:].strip()
        for sep in (";", "&&"):
            idx = rest.find(sep)
            if idx != -1:
                chained = rest[idx + len(sep):].strip()
                # Execute the cd first (done above), then run the chained command
                if chained and ok:
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/plain")
                    handler.send_header("Transfer-Encoding", "chunked")
                    handler.end_headers()
                    _run_process(handler, chained)
                    return
                break

        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write((msg + "\n").encode())
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    _run_process(handler, raw_cmd)


def _run_process(handler: "BaseHTTPRequestHandler", raw_cmd: str) -> None:
    global _active_pid
    cmd = preprocess(raw_cmd)
    process = None

    try:
        process = subprocess.Popen(
            f"export PAGER=cat; {cmd}",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            cwd=_current_dir,
            preexec_fn=os.setsid,
        )

        with _pid_lock:
            _active_pid = process.pid

        _spawn_auto_input(process)

        try:
            for line in process.stdout:
                _send_chunk(handler, line)
            process.wait(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                pass
            process.kill()
            process.wait()
            _send_chunk(handler, f"\n⏱️ Timed out after {COMMAND_TIMEOUT}s\n")

        if process.returncode and process.returncode != 0:
            _send_chunk(handler, f"\n❌ Exit code: {process.returncode}\n")
        else:
            _send_chunk(handler, "\n✅ Done\n")

    except Exception as e:
        _send_chunk(handler, f"\n❌ Error: {e}\n")
    finally:
        with _pid_lock:
            _active_pid = None
        _finalize_chunks(handler)
