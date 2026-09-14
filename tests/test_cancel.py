"""Cancellation must work across threads — that is the whole point.

`/cancel` arrives on its own HTTP thread (ThreadingMixIn), so anything it needs
to see has to be process-wide. Until 0.10.2 the running pid lived in
`threading.local()`, which meant `/cancel` read `active_pid` from a thread that
had never run a command, got `None`, and killed nothing: the endpoint could not
work over HTTP at all, which is the only transport the REST API has.

These tests run the command on one thread and cancel from another, so a
regression to thread-local state fails here rather than on a user's phone.
"""

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
    """No pid may leak between tests."""
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
    )


def test_registry_is_visible_from_another_thread():
    """The regression that broke /cancel: thread-local state is invisible here."""
    seen = {}
    proc = _spawn_sleeper()
    try:
        def run():
            shell.register_active_pid(proc.pid)
            time.sleep(5)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.5)

        # The thread that would serve /cancel.
        seen["local"] = shell.get_active_pid()
        seen["registry"] = shell.active_pids()
    finally:
        proc.kill()
        proc.wait(timeout=5)

    # get_active_pid() now reads the shared registry, so it reports the
    # running command from any thread. It previously read a thread-local that
    # only the running request's thread had written, which meant /env reported
    # active_command_pid: null no matter what was executing.
    assert seen["local"] == proc.pid, (
        "get_active_pid() must report the running command from any thread"
    )
    assert proc.pid in seen["registry"], "the registry must be process-wide"


def test_cancel_stops_a_running_command():
    proc = _spawn_sleeper()
    shell.register_active_pid(proc.pid)
    try:
        assert shell.cancel_active() is True
        # SIGTERM then a bounded grace, then SIGKILL.
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

        # And an already-finished command cannot be cancelled by a stale entry.
        assert shell.cancel_active() is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_cancel_signals_the_group_where_the_platform_has_it():
    """Each command runs under `setsid`, so its pid is its group id and killing
    the group reaches the command's children. Skipped where killpg is absent
    (Windows), because that is not the platform this ships on."""
    if not hasattr(os, "killpg"):
        pytest.skip("no process groups on this platform")

    # A child that outlives its parent unless the *group* is signalled.
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
        # Nothing left in that group.
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cancel_refuses_a_pid_the_daemon_did_not_start():
    """Cancelling must not become a way to kill arbitrary processes.

    The registry only holds commands this daemon started. Naming any other
    pid has to be refused, or /cancel is a general-purpose kill.
    """
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
    """A scoped cancel must leave other running commands alone."""
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
                # killpg is Unix-only; this suite also runs on Windows, where
                # reaching for it raises AttributeError rather than OSError.
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
