import socket
import subprocess
import time

from .config import PORT_POLL_INTERVAL


def kill_port(port: int) -> None:
    try:
        result = subprocess.run(
            f"lsof -t -i:{port}",
            shell=True,
            capture_output=True,
            text=True,
        )
        for pid in result.stdout.strip().splitlines():
            if pid:
                subprocess.run(f"kill -9 {pid}", shell=True, check=False)
    except Exception:
        pass

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(PORT_POLL_INTERVAL)
