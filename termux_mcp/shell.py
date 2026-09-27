import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from .config import AUTO_INPUT_INTERVAL, HOME, MAX_OUTPUT_BYTES
from .security import get_risk_assessment
from .utils import (expand_home, is_install_command, json_response,
                    kill_process_group, split_cd_chain)

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

_cwd_lock = threading.Lock()
_current_dir: str = os.getcwd()


def get_current_dir() -> str:
    with _cwd_lock:
        return _current_dir


def set_current_dir(path: str) -> None:
    global _current_dir
    with _cwd_lock:
        _current_dir = path


_active_pids: set = set()
_active_pids_lock = threading.Lock()

CANCEL_GRACE = 0.4


def register_active_pid(pid: int) -> None:
    with _active_pids_lock:
        _active_pids.add(pid)


def unregister_active_pid(pid: int) -> None:
    with _active_pids_lock:
        _active_pids.discard(pid)


def get_active_pid() -> Optional[int]:
    with _active_pids_lock:
        return next(iter(_active_pids), None)


def active_pids() -> list:
    with _active_pids_lock:
        return sorted(_active_pids)


def cancel_active(pid: Optional[int] = None) -> bool:
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

    if killed:
        time.sleep(CANCEL_GRACE)
        force = getattr(signal, "SIGKILL", signal.SIGTERM)
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
    path_part, _ = split_cd_chain(raw_cmd[2:].strip())

    if not path_part or path_part == "~":
        set_current_dir(HOME)
        return True, HOME

    raw_path = expand_home(path_part.strip(), HOME)
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
    raw_cmd = raw_cmd.strip()

    from . import kernel
    decision = kernel.gate(raw_cmd, enforce_warning=False,
                           tool=getattr(handler, "_kernel_tool", "run"),
                           principal=getattr(handler, "principal", None),
                           cwd=get_current_dir())
    if not decision["allow"]:
        json_response(handler, decision["http"], kernel.refusal_payload(decision))
        return

    if raw_cmd.startswith("cd"):
        _path, chained = split_cd_chain(raw_cmd[2:].strip())
        ok, msg = handle_cd(raw_cmd)
        if chained:
            if ok:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/plain")
                handler.send_header("Transfer-Encoding", "chunked")
                handler.end_headers()
                _run_process(handler, chained, stdin_data)
                return

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


def run_captured(cmd: str, confirmed: bool = False, task_id: str = "") -> dict:
    from .mcp_bridge import VirtualHandler, decode_virtual

    raw = str(cmd or "").strip()
    if not raw:
        return {"ok": False, "reason": "empty", "text": "No command."}

    from . import kernel
    decision = kernel.gate(raw, confirmed=confirmed, cwd=get_current_dir())
    if decision["status"] == "confirm":
        return {"ok": False, "reason": "confirmation_required",
                "risk_level": decision["risk_level"], "text": decision["message"]}
    if not decision["allow"]:
        return {"ok": False, "reason": decision["status"], "code": decision["code"],
                "risk_level": decision["risk_level"], "text": decision["message"]}

    _, snapshots = kernel.prepare(raw, task_id, echo=False)
    handler = VirtualHandler()
    execute_streaming(handler, raw)
    result = decode_virtual(handler)
    return {"ok": not result.get("is_error"), "reason": "",
            "text": result.get("text", ""), "snapshots": snapshots}


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
        from . import kernel
        popen_kwargs.update(kernel.popen_kwargs())
        limit = kernel.timeout_for(raw_cmd)

        process = subprocess.Popen(f"export PAGER=cat; {cmd}", **popen_kwargs)

        register_active_pid(process.pid)

        if stdin_data is not None:
            _feed_stdin(process, stdin_data)
        else:
            _spawn_auto_input(process, raw_cmd)

        watchdog = kernel.arm_watchdog(process, limit, killed.set)

        for line in process.stdout:
            if killed.is_set():
                break
            sent_bytes += len(line.encode())
            if sent_bytes <= MAX_OUTPUT_BYTES:
                _send_chunk(handler, line)
            elif not truncated:
                truncated = True
                _send_chunk(handler, truncation_marker)

        if watchdog is not None:
            watchdog.join(timeout=2)
        if killed.is_set():
            _send_chunk(handler, f"\n⏱️ Timed out after {limit}s\n")

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
            kill_process_group(process)
            unregister_active_pid(process.pid)
        _finalize_chunks(handler)
