import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from termux_mcp import shell


@pytest.fixture(autouse=True)
def _clean_registry():
    with shell._active_pids_lock:
        shell._active_pids.clear()
    yield
    with shell._active_pids_lock:
        shell._active_pids.clear()


def _spawn_sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_registry_is_visible_from_another_thread():
    seen = {}
    proc = _spawn_sleeper()
    try:
        def run():
            shell.register_active_pid(proc.pid)
            time.sleep(5)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.5)

        seen["local"] = shell.get_active_pid()
        seen["registry"] = shell.active_pids()
    finally:
        proc.kill()
        proc.wait(timeout=5)

    assert seen["local"] == proc.pid, (
        "get_active_pid() must report the running command from any thread"
    )
    assert proc.pid in seen["registry"], "the registry must be process-wide"


def test_cancel_stops_a_running_command():
    proc = _spawn_sleeper()
    shell.register_active_pid(proc.pid)
    try:
        assert shell.cancel_active() is True
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "the command survived cancel_active()"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_with_nothing_running_is_false_not_an_error():
    assert shell.active_pids() == []
    assert shell.cancel_active() is False


def test_unregister_removes_the_pid():
    proc = _spawn_sleeper()
    try:
        shell.register_active_pid(proc.pid)
        assert proc.pid in shell.active_pids()

        shell.unregister_active_pid(proc.pid)
        assert proc.pid not in shell.active_pids()

        assert shell.cancel_active() is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_cancel_signals_the_group_where_the_platform_has_it():
    if not hasattr(os, "killpg"):
        pytest.skip("no process groups on this platform")

    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    shell.register_active_pid(proc.pid)
    try:
        assert shell.cancel_active() is True
        time.sleep(1.0)
        assert proc.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cancel_refuses_a_pid_the_daemon_did_not_start():
    victim = subprocess.Popen(
        ["sh", "-c", "sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        assert shell.cancel_active(victim.pid) is False
        time.sleep(0.3)
        assert victim.poll() is None, "an unregistered pid was signalled"
    finally:
        victim.kill()
        victim.wait(timeout=5)


def test_cancel_with_a_pid_stops_only_that_command():
    first = _spawn_sleeper()
    second = _spawn_sleeper()
    shell.register_active_pid(first.pid)
    shell.register_active_pid(second.pid)
    try:
        assert shell.cancel_active(first.pid) is True
        time.sleep(0.8)
        assert first.poll() is not None, "the named command did not stop"
        assert second.poll() is None, "an unrelated command was killed too"
    finally:
        for p in (first, second):
            try:
                if hasattr(os, "killpg"):
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
