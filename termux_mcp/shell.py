import os
import subprocess
import threading
import time
import signal
from typing import TYPE_CHECKING

from .config import AUTO_INPUT_INTERVAL, AUTO_YES_COMMANDS, HOME

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


_current_dir: str = os.getcwd()


def get_current_dir() -> str:
    return _current_dir


def set_current_dir(path: str) -> None:
    global _current_dir
    _current_dir = path


   
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


   
def handle_cd(parts: list[str]) -> tuple[bool, str]:
    if len(parts) == 1:
        set_current_dir(HOME)
        return True, f"📂 {_current_dir}"

    raw_path = parts[1].strip().replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path) else os.path.join(_current_dir, raw_path)
    )

    if os.path.isdir(new_path):
        set_current_dir(new_path)
        return True, f"📂 {_current_dir}"

    return False, f"❌ Directory not found: {new_path}"


 
def _send_chunk(handler: "BaseHTTPRequestHandler", text: str) -> None:
    data = text.encode()
    size = hex(len(data))[2:].encode()
    handler.wfile.write(size + b"\r\n" + data + b"\r\n")
    handler.wfile.flush()


def _finalize_chunks(handler: "BaseHTTPRequestHandler") -> None:
    handler.wfile.write(b"0\r\n\r\n")


 
def _spawn_auto_input(process: subprocess.Popen) -> None:
    def _worker():
        try:
            while process.poll() is None:
                time.sleep(AUTO_INPUT_INTERVAL)
                if process.stdin:
                    try:
                        process.stdin.write("y\n")
                        process.stdin.flush()
                    except:
                        break
        except:
            pass

    threading.Thread(target=_worker, daemon=True).start()


 
def kill_all_servers():
    """
    Kill ALL possible running servers / ports
    """
    commands = [
        "pkill -9 -f 8080",
        "pkill -9 -f node",
        "pkill -9 -f python",
        "pkill -9 -f php",
        "pkill -9 -f uvicorn",
        "pkill -9 -f flask",
        "pkill -9 -f http.server",
        "fuser -k 8080/tcp",
    ]

    for cmd in commands:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


 
def execute_streaming(handler: "BaseHTTPRequestHandler", raw_cmd: str) -> None:
    raw_cmd = raw_cmd.strip()

    # Handle cd separately
    if raw_cmd.startswith("cd"):
        ok, msg = handle_cd(raw_cmd.split(maxsplit=1))
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write((msg + "\n").encode())
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

     _send_chunk(handler, "🛑 Killing running servers...\n")
    kill_all_servers()

     cmd = preprocess(raw_cmd)
    _send_chunk(handler, f"⚙️ Running: {cmd}\n\n")

     process = subprocess.Popen(
        f"export PAGER=cat; {cmd}",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        cwd=_current_dir,
        preexec_fn=os.setsid
    )

    _spawn_auto_input(process)

     try:
        while True:
            line = process.stdout.readline()

            if not line and process.poll() is not None:
                break

            if line:
                _send_chunk(handler, line)

         try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            _send_chunk(handler, "\nProcess killed (timeout)\n")

    except Exception as e:
        _send_chunk(handler, f"\nError: {str(e)}\n")

    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except:
            pass

     if process.returncode == 0:
        _send_chunk(handler, "\nDone\n")
    else:
        _send_chunk(handler, f"\nExit code: {process.returncode}\n")

    _finalize_chunks(handler)
