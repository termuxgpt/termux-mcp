
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import changes, safety, websocket

class FakeSock:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

class TestWebSocketParity:

    def setup_method(self):
        self.home = tempfile.mkdtemp(prefix="mcp-parity-")
        self.previous = safety.HOME
        safety.HOME = self.home
        self.root = os.path.join(self.home, "termuxGPT")
        self.ran = []
        self._real_runner = websocket._ws_run_process
        websocket._ws_run_process = (
            lambda sock, cmd, conn: (self.ran.append(cmd), "stub output")[1])

    def teardown_method(self):
        websocket._ws_run_process = self._real_runner
        safety.HOME = self.previous
        shutil.rmtree(self.home, ignore_errors=True)

    def target(self, name, text=None):
        path = os.path.join(self.home, name)
        if text is not None:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return path

    def call(self, tool, params):
        conn = {"send_lock": threading.Lock()}
        websocket._ws_execute_tool(FakeSock(), tool, params, conn, 7)
        return changes.read(self.root)

    def test_a_websocket_write_is_snapshotted_and_journalled(self):
        path = self.target("notes.txt", "before")
        entries = self.call("write", {"path": path, "content": "after"})
        assert entries[0]["kind"] == changes.MODIFY
        assert entries[0]["tool"] == "write"
        assert os.path.exists(entries[0]["snapshot"])
        with open(entries[0]["snapshot"], encoding="utf-8") as handle:
            assert handle.read() == "before"

    def test_a_websocket_write_of_a_new_file_is_a_creation(self):
        path = self.target("fresh.txt")
        entries = self.call("write", {"path": path, "content": "new"})
        assert entries[0]["kind"] == changes.CREATE
        assert "snapshot" not in entries[0]

    def test_a_websocket_delete_trashes_instead_of_removing(self):
        path = self.target("gone.txt", "precious")
        entries = self.call("delete", {"path": path})
        assert not os.path.exists(path)
        assert entries[0]["kind"] == changes.DELETE
        with open(entries[0]["trash"], encoding="utf-8") as handle:
            assert handle.read() == "precious"

    def test_a_websocket_delete_runs_no_shell_command_at_all(self):
        path = self.target("gone.txt", "x")
        self.call("delete", {"path": path})
        assert self.ran == [], "a delete must not be a shell rm any more"

    def test_the_write_itself_still_reaches_the_shell(self):
        path = self.target("notes.txt", "before")
        self.call("write", {"path": path, "content": "after"})
        assert any("base64 -d" in cmd for cmd in self.ran)
        assert path in self.ran[0]

    def test_a_delete_outside_home_is_still_refused(self):
        entries = self.call("delete", {"path": "relative/path.txt"})
        assert entries == []
