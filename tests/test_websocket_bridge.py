import json
import os
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
