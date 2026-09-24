import json
import os
import struct
import sys
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import approval
from termux_mcp import websocket as ws


def _read_frame(frame: bytes):
    length = frame[1] & 0x7F
    at = 2
    if length == 126:
        length = struct.unpack(">H", frame[2:4])[0]
        at = 4
    elif length == 127:
        length = struct.unpack(">Q", frame[2:10])[0]
        at = 10
    return frame[at:at + length]


class FakeSocket:
    def __init__(self):
        self.frames = []

    def sendall(self, data):
        self.frames.append(data)

    def last(self):
        return json.loads(_read_frame(self.frames[-1]).decode())


def _conn():
    return {"cwd": os.getcwd(), "active_pid": None,
            "killed": threading.Event(), "lock": threading.RLock(),
            "send_lock": threading.Lock()}


def _call(tool, params=None):
    sock = FakeSocket()
    ws._ws_execute_tool(sock, tool, params or {}, _conn(), "req_1")
    return sock.last()


class TestRiskGate:

    def setup_method(self):
        approval._held.clear()

    def _run_cmd(self, params, req_id="req_1"):
        sock = FakeSocket()
        with mock.patch.object(ws, "_ws_run_process",
                               return_value="done") as runner:
            ws._ws_execute_tool(sock, "run", params, _conn(), req_id)
        return sock.last(), runner

    def test_a_blocked_command_never_reaches_the_shell(self):
        reply, runner = self._run_cmd({"cmd": "rm -rf /"})
        assert runner.call_count == 0
        assert "Blocked" in reply["error"]

    def test_a_warning_command_is_held_back(self):
        reply, runner = self._run_cmd({"cmd": "rm -rf /tmp/x"})
        assert runner.call_count == 0
        assert json.loads(reply["output"])["status"] == "confirmation_required"

    def test_confirmed_lets_a_warning_command_through(self):
        _, runner = self._run_cmd({"cmd": "rm -rf /tmp/x",
                                   "confirmed": True})
        assert runner.call_count == 1

    def test_a_device_approval_lets_it_through_once(self):
        approval.hold("rm -rf /tmp/x")
        _, runner = self._run_cmd({"cmd": "rm -rf /tmp/x"})
        assert runner.call_count == 1
        _, runner = self._run_cmd({"cmd": "rm -rf /tmp/x"})
        assert runner.call_count == 0

    def test_a_safe_command_is_not_touched(self):
        _, runner = self._run_cmd({"cmd": "ls -la"})
        assert runner.call_count == 1

    def test_a_legacy_client_is_refused_in_plain_text(self):
        sock = FakeSocket()
        with mock.patch.object(ws, "_ws_run_process", return_value="done"):
            ws._ws_execute_tool(sock, "run", {"cmd": "rm -rf /"}, _conn(), None)
        text = _read_frame(sock.frames[-1]).decode()
        assert "Blocked" in text
        assert not text.lstrip().startswith("{")


class TestBridgeFallback:

    def test_a_bridge_tool_the_ws_never_had_is_reachable(self):
        reply = _call("changes_list", {"format": "json", "limit": 5})
        assert "_id" not in reply["output"]
        assert "changes" in json.loads(reply["output"])

    def test_the_reply_carries_the_request_id(self):
        reply = _call("changes_list", {"format": "json"})
        assert reply["_id"] == "req_1"

    def test_a_prose_bridge_tool_comes_back_as_text(self):
        reply = _call("changes_list", {"limit": 5})
        assert reply["is_error"] is False

    def test_another_bridge_tool_is_reachable_too(self):
        reply = _call("recipe_list")
        assert "error" not in reply

    def test_an_unknown_tool_still_says_so(self):
        reply = _call("no_such_tool_at_all")
        assert reply == {"error": "Unknown tool: no_such_tool_at_all",
                         "_id": "req_1"}

class TestExistingBranchesStillWin:

    def test_terminal_tools_keep_their_own_branch(self):
        reply = _call("terminal_list")
        assert "output" in reply
        assert "error" not in reply

    def test_the_legacy_aliases_keep_their_own_branches(self):
        assert "error" not in _call("wifi")

    def test_the_run_branch_is_untouched(self):
        sock = FakeSocket()
        ws._ws_execute_tool(sock, "run", {"cmd": "echo hi"}, _conn(), None)
        assert sock.frames
