import base64
import hashlib
import json
import os
import signal
import struct
import subprocess
import threading
import time
from typing import Optional

from .config import AUTO_INPUT_INTERVAL, COMMAND_TIMEOUT, HOME
from .utils import is_install_command

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_current_dir = os.getcwd()
_active_pid: Optional[int] = None
_pid_lock = threading.Lock()


def set_cwd(path: str) -> None:
    global _current_dir
    _current_dir = path


def get_cwd() -> str:
    return _current_dir


def _make_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    frame = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        frame += bytes([length])
    elif length < 65536:
        frame += bytes([126]) + struct.pack(">H", length)
    else:
        frame += bytes([127]) + struct.pack(">Q", length)
    return frame + payload


def _read_frame(sock) -> tuple:
    b1 = sock.recv(1)
    if not b1:
        return None, None
    b2 = sock.recv(1)
    if not b2:
        return None, None
    opcode = b1[0] & 0x0F
    length = b2[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    masks = sock.recv(4)
    data = bytearray(sock.recv(length))
    for i in range(length):
        data[i] ^= masks[i % 4]
    return opcode, bytes(data)


def _do_handshake(sock, headers: str) -> bool:
    for line in headers.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + WS_MAGIC.decode()).encode()).digest()
            ).decode()
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            sock.sendall(response.encode())
            return True
    return False


def handle_cd(raw_cmd: str) -> tuple:
    rest = raw_cmd[2:].strip()
    path_part = rest
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1:
            path_part = rest[:idx].strip()
            break
    if not path_part or path_part == "~":
        set_cwd(HOME)
        return True, HOME
    raw_path = path_part.strip().replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path) else os.path.join(get_cwd(), raw_path)
    )
    if os.path.isdir(new_path):
        set_cwd(new_path)
        return True, get_cwd()
    return False, f"Directory not found: {new_path}"


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


def ws_execute(sock, raw_cmd: str) -> None:
    global _active_pid
    raw_cmd = raw_cmd.strip()

    if raw_cmd.startswith("cd"):
        ok, msg = handle_cd(raw_cmd)
        rest = raw_cmd[2:].strip()
        for sep in (";", "&&"):
            idx = rest.find(sep)
            if idx != -1:
                chained = rest[idx + len(sep):].strip()
                if chained and ok:
                    sock.sendall(_make_frame(f"cd: {msg}\n".encode()))
                    raw_cmd = chained
                    break
        else:
            sock.sendall(_make_frame(f"{msg}\n".encode()))
            return

    cmd = f"export DEBIAN_FRONTEND=noninteractive; {raw_cmd}"
    process = None
    killed = threading.Event()

    try:
        kwargs = dict(shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      stdin=subprocess.PIPE, text=True, cwd=get_cwd())
        if hasattr(os, "setsid"):
            kwargs["preexec_fn"] = os.setsid
        process = subprocess.Popen(f"export PAGER=cat; {cmd}", **kwargs)

        with _pid_lock:
            _active_pid = process.pid
        _spawn_auto_input(process, raw_cmd)

        def _timeout_watchdog() -> None:
            try:
                process.wait(timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                killed.set()
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                        time.sleep(1)
                    process.kill()
                except Exception:
                    process.kill()

        watchdog = threading.Thread(target=_timeout_watchdog, daemon=True)
        watchdog.start()

        for line in process.stdout:
            try:
                sock.sendall(_make_frame(line.encode()))
            except Exception:
                break
            if killed.is_set():
                try:
                    sock.sendall(_make_frame(f"\nTimed out after {COMMAND_TIMEOUT}s\n".encode()))
                except Exception:
                    pass
                break

        watchdog.join(timeout=2)
        if not killed.is_set():
            tag = "Done" if process.returncode == 0 else f"Exit: {process.returncode}"
            try:
                sock.sendall(_make_frame(f"\n{tag}\n".encode()))
            except Exception:
                pass
    except Exception as e:
        try:
            sock.sendall(_make_frame(f"\nError: {e}\n".encode()))
        except Exception:
            pass
    finally:
        with _pid_lock:
            _active_pid = None


def ws_handler(sock, raw_headers: str) -> None:
    if not _do_handshake(sock, raw_headers):
        sock.close()
        return

    set_cwd(HOME)

    try:
        while True:
            opcode, data = _read_frame(sock)
            if opcode is None or opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                sock.sendall(_make_frame(data, OP_PONG))
                continue
            if opcode == OP_TEXT and data:
                try:
                    msg = json.loads(data.decode())
                except json.JSONDecodeError:
                    continue
                cmd = msg.get("cmd", "")
                if cmd:
                    ws_execute(sock, cmd)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
