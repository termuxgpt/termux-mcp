import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import base, cache, intent_tools, reactor


@pytest.fixture
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="p2-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    yield h
    shutil.rmtree(h, ignore_errors=True)


class TestWatchdog:
    def test_service_down_trigger(self, home, monkeypatch):
        monkeypatch.setattr(intent_tools, "service_table", lambda: [("bot", "python bot.py", 0, None, ""),
                                                                    ("web", "npm start", 3000, 4242, "")])
        assert reactor.evaluate({"service_down": "bot"}, {}, {})
        assert not reactor.evaluate({"service_down": "web"}, {}, {})
        assert not reactor.evaluate({"service_down": "ghost"}, {}, {})

    def test_restart_action_calls_service_ensure(self, home, monkeypatch):
        calls = []
        monkeypatch.setattr(intent_tools, "service_ensure", lambda name, restart=False, **k: calls.append((name, restart)) or {"up": True})
        rule = {"action": {"restart_service": "bot"}}
        out = reactor._fire(rule)
        assert calls == [("bot", True)] and "restart ok" in out and rule["_last_match"] == "restarted"

    def test_service_watch_add_dedupe_stop(self, home, monkeypatch):
        monkeypatch.setattr(intent_tools, "service_table", lambda: [("bot", "python bot.py", 0, 11, "")])
        monkeypatch.setattr(reactor, "ensure_running", lambda: None)
        first = intent_tools.service_watch("bot")
        assert first["ok"]
        again = intent_tools.service_watch("bot")
        assert "already watched" in again["summary"]
        rules = [r for r in reactor._rules()["rules"] if r["when"].get("service_down") == "bot"]
        assert len(rules) == 1 and rules[0]["action"]["restart_service"] == "bot"
        assert intent_tools.service_watch("bot", action="stop")["removed"] == 1
        assert intent_tools.service_watch("nope")["errors"][0]["code"] == "E_NO_SERVICE"

    def test_overview_marks_watched(self, home, monkeypatch):
        monkeypatch.setattr(intent_tools, "service_table", lambda: [("bot", "python bot.py", 8000, 11, "")])
        monkeypatch.setattr(reactor, "ensure_running", lambda: None)
        intent_tools.service_watch("bot")
        o = intent_tools.services_overview()
        [svc] = o["services"]
        assert svc["up"] and svc["watched"] and svc["url"] == "http://localhost:8000"


def _posix_sh(cmd, timeout=None, cwd=None, confirmed=False, check_risk=True, stdin=None):
    bash = shutil.which("bash") or "sh"
    r = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return base.ShellResult(cmd, r.returncode, r.stdout, r.stderr, 0.0)


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
class TestGitCommit:
    @pytest.fixture(autouse=True)
    def posix(self, monkeypatch):
        monkeypatch.setattr(base, "sh", _posix_sh)

    def repo(self, home):
        r = os.path.join(home, "proj").replace(os.sep, "/")
        os.makedirs(r)
        run = lambda *a: subprocess.run(["git", "-C", r, *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        with open(os.path.join(r, "a.txt"), "w") as f:
            f.write("one\n")
        run("add", "-A")
        run("commit", "-qm", "init")
        return r

    def test_flow_message_preview_commit(self, home):
        r = self.repo(home)
        with open(os.path.join(r, "a.txt"), "a") as f:
            f.write("two\n")
        step1 = intent_tools.git_commit(r)
        assert step1["needs_message"] and "two" in step1["diff_excerpt"]
        step2 = intent_tools.git_commit(r, "Add second line")
        assert step2["needs_confirmation"] and step2["file_count"] == 1
        done = intent_tools.git_commit(r, "Add second line", confirmed=True)
        assert done["ok"] and done["commit"]
        log = subprocess.run(["git", "-C", r, "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
        assert log == "Add second line"
        assert intent_tools.git_commit(r)["nothing_to_commit"]

    def test_not_a_repo(self, home):
        assert intent_tools.git_commit(home)["errors"][0]["code"] == "E_NOT_REPO"
