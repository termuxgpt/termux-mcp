import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any, cast

from termux_mcp import approval
from termux_mcp.approval import held, hold, key, run_approval_tool, spend
from termux_mcp.handler import MCPHandler
from termux_mcp.mcp_bridge import VirtualHandler, decode_virtual


def _done(text: str):
    return mock.Mock(stdout=text, stderr="", returncode=0)


def _fingerprint(result: str, errors=None):
    return _done(json.dumps({"errors": errors or [], "failed_attempts": 0,
                             "auth_result": result}))


def _status(text: str) -> str:
    body = text.split(approval.APPROVAL_MARKER, 1)[1]
    return json.loads(body.strip().split("\n")[0])["status"]


class TestKey:

    def test_whitespace_collapsed(self):
        assert key("pkg  upgrade\n -y") == "pkg upgrade -y"

    def test_missing_action_is_empty(self):
        assert key(None) == ""
        assert key("   ") == ""


class TestFingerprint:

    def setup_method(self):
        approval._held.clear()

    def test_success_approves_and_holds(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_SUCCESS")):
            out = run_approval_tool("approve",
                                    {"action": "rm -rf build"})
        assert out["is_error"] is False
        assert "rm -rf build" in out["text"]
        assert held("rm -rf build")

    def test_failed_attempt_declines(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_FAILURE")):
            out = run_approval_tool("approve", {"action": "rm -rf build"})
        assert out["is_error"] is True
        assert "did not approve" in out["text"]
        assert not held("rm -rf build")

    def test_unknown_result_declines(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_UNKNOWN")):
            out = run_approval_tool("approve", {"action": "rm -rf build"})
        assert out["is_error"] is True
        assert not held("rm -rf build")

    def test_no_enrolled_fingerprint_falls_back_to_dialog(self):
        dialog = _done(json.dumps({"code": -1, "text": ""}))
        with mock.patch("subprocess.run") as run:
            run.side_effect = [
                _fingerprint("AUTH_RESULT_FAILURE",
                             ["ERROR_NO_ENROLLED_FINGERPRINTS"]),
                dialog,
            ]
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert out["is_error"] is False
        assert held("pkg upgrade")
        assert run.call_count == 2

    def test_explicit_fingerprint_does_not_fall_back(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = _fingerprint("AUTH_RESULT_FAILURE",
                                            ["ERROR_NO_HARDWARE"])
            out = run_approval_tool("approve", {"action": "pkg upgrade",
                                                "method": "fingerprint"})
        assert out["is_error"] is True
        assert "no fingerprint" in out["text"]
        assert run.call_count == 1

    def test_missing_termux_api_is_reported(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert out["is_error"] is True
        assert "termux-api" in out["text"]

    def test_timeout_is_reported(self):
        import subprocess
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("x", 1)):
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert out["is_error"] is True
        assert "could not ask" in out["text"]


class TestDialog:

    def setup_method(self):
        approval._held.clear()

    def test_positive_button_approves(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -1}))):
            out = run_approval_tool("approve", {"action": "pkg upgrade",
                                                "method": "dialog"})
        assert out["is_error"] is False
        assert held("pkg upgrade")

    def test_negative_button_declines(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -2}))):
            out = run_approval_tool("approve", {"action": "pkg upgrade",
                                                "method": "dialog"})
        assert out["is_error"] is True
        assert not held("pkg upgrade")

    def test_dialog_error_is_reported(self):
        payload = json.dumps({"code": -1, "error": "Unknown Input Method"})
        with mock.patch("subprocess.run", return_value=_done(payload)):
            out = run_approval_tool("approve", {"action": "pkg upgrade",
                                                "method": "dialog"})
        assert out["is_error"] is True
        assert "Unknown Input Method" in out["text"]


class TestArguments:

    def test_missing_action(self):
        out = run_approval_tool("approve", {})
        assert out["is_error"] is True
        assert "Missing 'action'" in out["text"]

    def test_cmd_is_accepted_as_action(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_SUCCESS")):
            out = run_approval_tool("approve", {"cmd": "pkg upgrade"})
        assert out["is_error"] is False

    def test_unknown_method_is_treated_as_auto(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_SUCCESS")):
            out = run_approval_tool("approve", {"action": "ls",
                                                "method": "telepathy"})
        assert out["is_error"] is False

    def test_long_reason_is_trimmed(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = _fingerprint("AUTH_RESULT_SUCCESS")
            run_approval_tool("approve", {"action": "ls",
                                          "reason": "x" * 500})
        argv = run.call_args[0][0]
        assert len(argv[argv.index("-d") + 1]) == 200

    def test_unknown_tool_name(self):
        assert run_approval_tool("approve_please", {})["is_error"] is True


class TestOutcomeMarker:

    def setup_method(self):
        approval._held.clear()

    def test_granted(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_SUCCESS")):
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert _status(out["text"]) == approval.GRANTED
        assert out["is_error"] is False

    def test_declined(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_FAILURE")):
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert _status(out["text"]) == approval.DECLINED
        assert out["is_error"] is True

    def test_unavailable(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            out = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert _status(out["text"]) == approval.UNAVAILABLE
        assert out["is_error"] is True

    def test_declined_and_unavailable_are_distinguishable(self):
        with mock.patch("subprocess.run",
                        return_value=_fingerprint("AUTH_RESULT_FAILURE")):
            declined = run_approval_tool("approve", {"action": "pkg upgrade"})
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            unavailable = run_approval_tool("approve", {"action": "pkg upgrade"})
        assert declined["is_error"] == unavailable["is_error"] is True
        assert _status(declined["text"]) != _status(unavailable["text"])

    def test_a_programmer_error_carries_no_marker(self):
        assert approval.APPROVAL_MARKER not in run_approval_tool("approve",
                                                                 {})["text"]


class TestAsk:

    def _argv(self, params):
        with mock.patch("subprocess.run") as run:
            run.return_value = _done(json.dumps({"code": -1, "text": "ok"}))
            approval.run_ask_tool(params)
        return run.call_args[0][0]

    def test_text_answer(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -1,
                                                       "text": "8080"}))):
            out = approval.run_ask_tool({"widget": "text",
                                         "title": "Which port?"})
        assert out["is_error"] is False
        assert "8080" in out["text"]

    def test_title_and_hint(self):
        argv = self._argv({"widget": "text", "title": "Where?",
                           "hint": "a folder"})
        assert argv == ["termux-dialog", "text", "-t", "Where?",
                        "-i", "a folder"]

    def test_defaults_to_text(self):
        assert self._argv({})[1] == "text"

    def test_number_gets_the_numeric_flag(self):
        assert "-n" in self._argv({"widget": "number"})

    def test_multiline_only_applies_to_text(self):
        assert "-m" in self._argv({"widget": "text", "multiline": True})
        assert "-m" not in self._argv({"widget": "speech",
                                       "multiline": True})

    def test_hint_is_not_sent_to_choice_widgets(self):
        argv = self._argv({"widget": "radio", "values": ["a", "b"],
                           "hint": "pick one"})
        assert "-i" not in argv

    def test_choices_are_comma_joined(self):
        argv = self._argv({"widget": "radio", "values": ["a", "b", "c"]})
        assert argv[argv.index("-v") + 1] == "a,b,c"

    def test_a_comma_inside_a_choice_is_escaped(self):
        argv = self._argv({"widget": "radio",
                           "values": ["Arch, rolling", "Debian stable"]})
        assert argv[argv.index("-v") + 1] == "Arch\\, rolling,Debian stable"

    def test_a_backslash_in_a_choice_is_refused(self):
        out = approval.run_ask_tool({"widget": "radio",
                                     "values": ["C:\\Users"]})
        assert out["is_error"] is True
        assert "backslash" in out["text"]

    def test_choices_are_required(self):
        out = approval.run_ask_tool({"widget": "spinner"})
        assert out["is_error"] is True
        assert "values" in out["text"]

    def test_checkbox_returns_every_tick(self):
        payload = {"code": -1, "text": "",
                   "values": [{"index": 0, "text": "logs"},
                              {"index": 2, "text": "cache"}]}
        with mock.patch("subprocess.run", return_value=_done(json.dumps(payload))):
            out = approval.run_ask_tool({"widget": "checkbox",
                                         "values": ["logs", "tmp", "cache"]})
        assert out["is_error"] is False
        assert "logs, cache" in out["text"]

    def test_date_takes_a_format(self):
        argv = self._argv({"widget": "date", "format": "dd-MM-yyyy"})
        assert argv[argv.index("-d") + 1] == "dd-MM-yyyy"

    def test_time_ignores_a_date_format(self):
        assert "-d" not in self._argv({"widget": "time",
                                       "format": "dd-MM-yyyy"})

    def test_cancel_is_reported(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -2}))):
            out = approval.run_ask_tool({"widget": "text"})
        assert out["is_error"] is True
        assert "cancelled" in out["text"]

    def test_ok_with_nothing_typed(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -1,
                                                       "text": ""}))):
            out = approval.run_ask_tool({"widget": "text"})
        assert out["is_error"] is True
        assert "no answer" in out["text"]

    def test_unknown_widget(self):
        out = approval.run_ask_tool({"widget": "hologram"})
        assert out["is_error"] is True
        assert "Unknown widget" in out["text"]

    def test_dialog_error_is_reported(self):
        payload = json.dumps({"code": 0, "error": "Unknown Input Method"})
        with mock.patch("subprocess.run", return_value=_done(payload)):
            out = approval.run_ask_tool({"widget": "text"})
        assert out["is_error"] is True
        assert "Unknown Input Method" in out["text"]

    def test_missing_termux_api(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            out = approval.run_ask_tool({"widget": "text"})
        assert out["is_error"] is True
        assert "termux-api" in out["text"]

    def test_the_ask_tool_is_reachable_by_name(self):
        with mock.patch("subprocess.run",
                        return_value=_done(json.dumps({"code": -1,
                                                       "text": "yes"}))):
            out = run_approval_tool("ask", {"widget": "text"})
        assert out["is_error"] is False


class TestHeldApprovals:

    def setup_method(self):
        approval._held.clear()

    def test_spend_is_one_shot(self):
        hold("rm -rf build")
        assert spend("rm -rf build") is True
        assert spend("rm -rf build") is False

    def test_whitespace_does_not_matter(self):
        hold("pkg  upgrade\n-y")
        assert spend("pkg upgrade -y") is True

    def test_a_different_action_is_not_approved(self):
        hold("pkg upgrade")
        assert spend("rm -rf /") is False

    def test_expiry(self):
        with mock.patch("time.time", return_value=1000.0):
            hold("pkg upgrade")
            assert held("pkg upgrade") is True
        with mock.patch("time.time",
                        return_value=1000.0 + approval.TTL_SECONDS):
            assert held("pkg upgrade") is False

    def test_old_entries_do_not_crowd_out_new_ones(self):
        with mock.patch("time.time", return_value=1000.0):
            for i in range(approval.MAX_HELD):
                hold(f"cmd {i}")
        with mock.patch("time.time", return_value=1100.0):
            hold("the new one")
            assert held("the new one") is True
            assert held("cmd 0") is False
            assert len(approval._held) == approval.MAX_HELD

    def test_blank_action_is_not_held(self):
        assert hold("   ") is False


def _leading_json(text: str) -> dict:
    body, _ = json.JSONDecoder().raw_decode(text.lstrip())
    return body


class TestRunNeedsApproval:

    def setup_method(self):
        approval._held.clear()

    def _run(self, data):
        handler = cast(Any, VirtualHandler())
        MCPHandler._handle_run(handler, data)
        return decode_virtual(handler)

    def test_warning_command_is_held_back(self):
        result = self._run({"cmd": "rm -rf /tmp/x"})
        payload = _leading_json(result["text"])
        assert payload["status"] == "confirmation_required"
        assert payload["requires_confirmation"] is True

    def test_the_hold_back_offers_the_device_approval(self):
        result = self._run({"cmd": "rm -rf /tmp/x"})
        assert "approve" in _leading_json(result["text"])["hint"]

    def test_a_held_approval_lets_it_through(self):
        hold("rm -rf /tmp/x")
        with mock.patch("termux_mcp.handler.execute_streaming") as run:
            self._run({"cmd": "rm -rf /tmp/x"})
        assert run.call_count == 1
        assert not held("rm -rf /tmp/x")

    def test_a_held_approval_for_something_else_does_not(self):
        hold("rm -rf /tmp/y")
        payload = _leading_json(self._run({"cmd": "rm -rf /tmp/x"})["text"])
        assert payload["status"] == "confirmation_required"
        assert held("rm -rf /tmp/y")

    def test_confirmed_still_works_without_an_approval(self):
        with mock.patch("termux_mcp.handler.execute_streaming") as run:
            self._run({"cmd": "rm -rf /tmp/x", "confirmed": True})
        assert run.call_count == 1

    def test_blocked_command_is_not_approved_by_a_hold(self):
        hold("rm -rf /")
        result = self._run({"cmd": "rm -rf /"})
        assert result["is_error"] is True
        assert "Blocked" in result["text"]
