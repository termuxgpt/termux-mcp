import socket
import subprocess


def kill_port(port: int) -> None:
    try:
        subprocess.run(
            f"pkill -9 -f {port}",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2
        )
    except:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                subprocess.run(
                    f"pkill -9 -f {port}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2
                )
    except:
        pass
