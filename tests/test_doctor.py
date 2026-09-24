import json
import os
import sys
from typing import Any, cast
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import playbook as pb
from termux_mcp.handlers.doctor import handle_doctor, handle_playbooks, render
from termux_mcp.mcp_bridge import VirtualHandler, decode_virtual, route_callable


def _finding(**over):
    base = {
        "id": "git_present", "title": "Git is installed", "severity": "high",
        "explain": "Git is needed to clone repositories.", "probe": "command -v git",
        "fix": {"playbook": "install_package", "inputs": {"package": "git"}},
        "ok": False, "detail": "git: not found", "code": 127,
    }
    base.update(over)
    return base


def _report(findings, unknown=None, errors=None):
    return {
        "ok": all(f["ok"] for f in findings),
        "checked": len(findings),
        "failing": len([f for f in findings if not f["ok"]]),
        "findings": findings,
        "unknown": unknown or [],
        "errors": errors or [],
    }


class TestRender:

    def test_a_clean_bill_of_health(self):
        text = render(_report([_finding(ok=True, detail="", code=0)]))
        assert "1 of 1 checks pass" in text
        assert "NEEDS ATTENTION" not in text
        assert "PASSING" in text

    def test_a_failure_lists_the_detail_the_explanation_and_the_fix(self):
        text = render(_report([_finding()]))
        assert "NEEDS ATTENTION" in text
        assert "[high] Git is installed (git_present)" in text
        assert "git: not found" in text
        assert "Git is needed to clone repositories." in text
        assert "Fix: install_package package=git" in text

    def test_a_silent_failure_shows_what_was_checked(self):
        text = render(_report([_finding(detail="")]))
        assert "checked with: command -v git" in text

    def test_unknown_ids_are_named(self):
        text = render(_report([], unknown=["ghost"]))
        assert "No such check: ghost" in text

    def test_library_problems_are_surfaced(self):
        text = render(_report([], errors=["sample: no title"]))
        assert "sample: no title" in text

    def test_a_check_with_no_fix_prints_no_fix_line(self):
        text = render(_report([_finding(fix=None, detail="x")]))
        assert "Fix:" not in text


class TestHandler:

    def _call(self, tool, data):
        handler = cast(Any, VirtualHandler())
        route_callable(tool)(handler, data)
        return decode_virtual(handler)

    def test_doctor_is_reachable(self):
        assert route_callable("doctor") is not None

    def test_playbooks_is_reachable(self):
        assert route_callable("playbooks") is not None

    def test_text_by_default(self):
        with mock.patch("termux_mcp.handlers.doctor.run_doctor",
                        return_value=_report([_finding()])):
            result = self._call("doctor", {"check": "git_present"})
        assert "NEEDS ATTENTION" in result["text"]
        assert result["is_error"] is False

    def test_json_when_asked(self):
        with mock.patch("termux_mcp.handlers.doctor.run_doctor",
                        return_value=_report([_finding()])):
            result = self._call("doctor", {"format": "json"})
        body = json.loads(result["text"])
        assert body["findings"][0]["id"] == "git_present"

    def test_a_comma_separated_list_becomes_a_filter(self):
        with mock.patch("termux_mcp.handlers.doctor.run_doctor",
                        return_value=_report([])) as run:
            self._call("doctor", {"check": "git_present, storage_linked"})
        assert run.call_args[1]["only"] == ["git_present", "storage_linked"]

    def test_no_filter_runs_everything(self):
        with mock.patch("termux_mcp.handlers.doctor.run_doctor",
                        return_value=_report([])) as run:
            self._call("doctor", {})
        assert run.call_args[1]["only"] == []

    def test_the_real_library_lists(self):
        body = json.loads(self._call("playbooks", {})["text"])
        ids = [p["id"] for p in body["playbooks"]]
        assert "clone_repo" in ids
        assert body["problems"] == []
        clone = [p for p in body["playbooks"] if p["id"] == "clone_repo"][0]
        assert clone["requires"] == ["git_present"]

    def test_one_playbook_in_full(self):
        body = json.loads(self._call("playbooks",
                                     {"playbook": "clone_repo"})["text"])
        assert body["playbook"]["id"] == "clone_repo"
        assert body["playbook"]["steps"]
        assert "_file" not in body["playbook"]

    def test_an_unknown_playbook_is_an_error(self):
        result = self._call("playbooks", {"playbook": "ghost"})
        assert result["is_error"] is True


class TestHandlerSurface:

    def test_the_doctor_changes_nothing(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            pb.run_doctor()
        for call in run.call_args_list:
            assert call[1].get("shell") is True
        assert run.call_count >= 10
