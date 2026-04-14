import os
import subprocess
import threading
import time
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
    """Prepend DEBIAN_FRONTEND=noninteractive so apt/pkg never stall."""
    return f"export DEBIAN_FRONTEND=noninteractive; {cmd}"


def _inject_auto_yes(cmd: str) -> str:
    """Add -y to package manager commands that don't already have it."""
    for trigger in AUTO_YES_COMMANDS:
        if trigger in cmd and "-y" not in cmd:
            cmd = cmd.replace(trigger, f"{trigger} -y")
    return cmd


def preprocess(cmd: str) -> str:
    cmd = _inject_auto_yes(cmd)
    cmd = _inject_noninteractive(cmd)
    return cmd


def handle_cd(parts: list[str]) -> tuple[bool, str]:
    """
    Parse a `cd` command.

    Returns (success, message).
    On success the global _current_dir is updated.
    """
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
    """Periodically write 'y\\n' to stdin so interactive prompts don't hang."""

    def _worker() -> None:
        try:
            while process.poll() is None:
                time.sleep(AUTO_INPUT_INTERVAL)
                process.stdin.write("y\n")
                process.stdin.flush()
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


_INSTALL_HINTS: list[tuple[str, str]] = [
    ("get:", "📦 Downloading packages...\n"),
    ("fetch", "📦 Downloading packages...\n"),
    ("unpacking", "⚙️  Installing dependencies...\n"),
    ("setting up", "⚙️  Finalizing setup...\n"),
]


def _install_hint(line: str) -> str | None:
    lower = line.lower()
    for keyword, hint in _INSTALL_HINTS:
        if keyword in lower:
            return hint
    if "error" in lower:
        return f"❌ {line.strip()}\n"
    return None


# APIs

def execute_streaming(handler: "BaseHTTPRequestHandler", raw_cmd: str) -> None:
    """
    Execute *raw_cmd* and stream output to *handler* using chunked transfer.

    Handles:
      - `cd` as a special built-in (no subprocess spawned)
      - Chunked HTTP streaming for all other commands
      - Auto-input thread for interactive prompts
      - Friendly progress hints during package installs
    """
    raw_cmd = raw_cmd.strip()

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

    cmd = preprocess(raw_cmd)
    is_install = "install" in cmd

    if is_install:
        _send_chunk(handler, "🚀 Processing request...\n\n")

    process = subprocess.Popen(
        f"export PAGER=cat; {cmd}",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        cwd=_current_dir,
    )

    _spawn_auto_input(process)

    for line in process.stdout:
        if is_install:
            hint = _install_hint(line)
            if hint:
                _send_chunk(handler, hint)
        else:
            _send_chunk(handler, line)

    process.wait()

    if process.returncode == 0:
        if is_install:
            _send_chunk(handler, "✅ Completed successfully\n")
    else:
        _send_chunk(handler, f"❌ Command failed (exit {process.returncode})\n")

    _finalize_chunks(handler)
