import json
import os
import shlex
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import automations, mcp_bridge, websocket
from termux_mcp.handlers import features


@pytest.fixture
def tab(monkeypatch):
    h = tempfile.mkdtemp(prefix="auto-home-")
    monkeypatch.setenv("HOME", h)
    state = {"text": ""}
    monkeypatch.setattr(automations, "crontab_available", lambda: True)
    monkeypatch.setattr(automations, "crond_running", lambda: True)
    monkeypatch.setattr(automations, "read_crontab", lambda: state["text"])

    def write(text):
        state["text"] = text
        return None

    monkeypatch.setattr(automations, "write_crontab", write)
    monkeypatch.setattr(automations, "gate", lambda command, confirmed: None)
    yield state
    shutil.rmtree(h, ignore_errors=True)


def jobs(state):
    return automations.parse(state["text"])[0]


class TestParse:
    def test_managed_job_round_trips(self, tab):
        r = automations.add("0 9 * * *", "echo hi", "Morning")
        assert r["ok"]
        [job] = jobs(tab)
        assert (job.label, job.schedule, job.command, job.managed) == ("Morning", "0 9 * * *", "echo hi", True)
        assert automations.render(*automations.parse(tab["text"])) == tab["text"]

    def test_foreign_lines_survive_untouched(self, tab):
        tab["text"] = "MAILTO=me\n# user note\n*/5 * * * * /usr/bin/true\n"
        automations.add("0 1 * * *", "date", "x")
        assert tab["text"].startswith("MAILTO=me\n# user note\n*/5 * * * * /usr/bin/true\n")
        assert len(jobs(tab)) == 2

    def test_legacy_label_comment_is_read_as_label(self, tab):
        tab["text"] = "# backup\n0 3 * * * tar czf b.tgz ~/code\n"
        [job] = jobs(tab)
        assert job.label == "backup" and not job.managed and job.command == "tar czf b.tgz ~/code"
        assert automations.render(*automations.parse(tab["text"])) == tab["text"]

    def test_cron_ensure_lines_are_recognised_and_kept_verbatim(self, tab):
        line = "*/15 * * * * df -h >> /h/termuxGPT/cron/disk.log 2>&1 # tmcp:disk"
        tab["text"] = line + "\n"
        [job] = jobs(tab)
        assert job.id == "tmcp-disk" and job.command == "df -h" and job.log == "/h/termuxGPT/cron/disk.log"
        assert automations.render(*automations.parse(tab["text"])) == tab["text"]


class TestMutations:
    def test_command_with_quotes_and_dollar_survives_quoting(self, tab):
        cmd = 'echo "it\'s $HOME" | tee -a ~/x.txt'
        automations.add("* * * * *", cmd, "q")
        [job] = jobs(tab)
        assert job.command == cmd
        wrapped = automations.wrap(job)
        assert cmd in shlex.split(wrapped)

    def test_rejects_bad_schedule_and_multiline(self, tab):
        assert automations.add("every day", "ls")["error"]
        assert automations.add("* * * * *", "ls\nrm -rf ~")["error"]
        assert tab["text"] == ""

    def test_pause_resume_and_remove(self, tab):
        jid = automations.add("0 * * * *", "uptime", "u")["job"]["id"]
        automations.toggle(jid, True)
        assert jobs(tab)[0].paused
        assert any(l.startswith(automations.PAUSED) for l in tab["text"].splitlines())
        automations.toggle(jid, False)
        assert not jobs(tab)[0].paused
        assert automations.remove(job_id=jid)["ok"]
        assert jobs(tab) == []

    def test_update_adopts_legacy_job(self, tab):
        tab["text"] = "# old\n0 3 * * * ls\n"
        old = jobs(tab)[0]
        r = automations.update(old.id, schedule="0 4 * * *")
        assert r["ok"]
        [job] = jobs(tab)
        assert job.managed and job.schedule == "0 4 * * *" and job.command == "ls" and job.id.isalnum()

    def test_missing_crontab_reports_setup(self, tab, monkeypatch):
        monkeypatch.setattr(automations, "crontab_available", lambda: False)
        r = automations.add("* * * * *", "ls")
        assert r["needs_setup"]
        assert automations.status() == {"installed": False, "running": False, "jobs": []}

    def test_risky_command_needs_confirmation(self, tab, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr(automations, "crontab_available", lambda: True)
        monkeypatch.setattr(automations, "read_crontab", lambda: "")
        monkeypatch.setattr(automations, "write_crontab", lambda t: None)
        r = automations.add("* * * * *", "rm -rf ~/projects")
        assert r.get("error") and (r.get("requires_confirmation") or r.get("blocked"))


class TestLogs:
    def test_last_run_and_exit_come_from_the_log(self, tab):
        jid = automations.add("* * * * *", "echo hi", "h")["job"]["id"]
        os.makedirs(automations.log_dir(), exist_ok=True)
        with open(automations.log_path(jid), "w") as f:
            f.write("first\n__tgpt_exit=0 1700000000\nboom\n__tgpt_exit=2 1700000060\n")
        [job] = automations.status()["jobs"]
        assert job["last_exit"] == 2 and job["last_run"] == 1700000060 and job["log_tail"] == ["boom"]


class TestRouting:
    def test_websocket_has_no_unquoted_cron_branch(self):
        src = open(websocket.__file__, encoding="utf-8").read()
        assert 'tool == "cron_add"' not in src
        assert mcp_bridge.route_callable("cron_add") is not None

    def test_new_tools_are_routed(self):
        for name in ("cron_status", "cron_toggle", "cron_update", "cron_setup"):
            assert mcp_bridge.route_callable(name) is not None

    def test_status_handler_returns_json(self, tab):
        automations.add("0 9 * * 1-5", "echo w", "Weekdays")
        vh = mcp_bridge.VirtualHandler()
        features.handle_cron_status(vh, {})
        data = json.loads(vh.decoded_text())
        assert data["installed"] and data["jobs"][0]["label"] == "Weekdays"
