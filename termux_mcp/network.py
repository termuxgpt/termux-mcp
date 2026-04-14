import subprocess


def kill_port(port: int):
    try:
        result = subprocess.run(
            f"lsof -ti:{port}",
            shell=True,
            capture_output=True,
            text=True
        )

        pids = result.stdout.strip().split("\n")

        for pid in pids:
            if pid:
                subprocess.run(
                    f"kill -9 {pid}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

    except:
        pass
