import io
import json
import os
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from termux_mcp import mcp_core as core


def _session():
    return core.MCPSession("test", "unittest")


def _dispatch(session, method, params=None) -> dict:
    result = core.dispatch(session, method, params)
    assert result is not None
    return result


class DispatchTests(unittest.TestCase):
    def test_server_info_shape(self):
        info = core.server_info()
        self.assertEqual(info["protocolVersion"], "2025-06-18")
        self.assertEqual(info["serverInfo"]["name"], "termux-native-mcp")
        self.assertIn("tools", info["capabilities"])

    def test_negotiate(self):
        self.assertEqual(core.negotiate_protocol("2025-06-18"), "2025-06-18")
        self.assertEqual(core.negotiate_protocol("2024-11-05"), "2025-06-18")
        self.assertEqual(core.negotiate_protocol(None), "2025-06-18")

    def test_dispatch_lifecycle(self):
        s = _session()
        res = _dispatch(s, "initialize",
                        {"protocolVersion": "2025-06-18"})
        self.assertEqual(s.protocol_version, "2025-06-18")
        self.assertIn("serverInfo", res)
        self.assertIsNone(core.dispatch(s, "notifications/initialized", {}))
        self.assertEqual(core.dispatch(s, "ping", {}), {})
        listing = _dispatch(s, "tools/list", {})
        names = [t["name"] for t in listing["tools"]]
        self.assertIn("run", names)
        self.assertIn("ls", names)

    def test_unknown_method(self):
        s = _session()
        with self.assertRaises(core.RpcError) as ctx:
            core.dispatch(s, "bogus/method", {})
        self.assertEqual(ctx.exception.code, core.METHOD_NOT_FOUND)

    def test_missing_tool_name(self):
        s = _session()
        with self.assertRaises(core.RpcError) as ctx:
            core.dispatch(s, "tools/call", {})
        self.assertEqual(ctx.exception.code, core.INVALID_PARAMS)

    def test_unknown_tool(self):
        s = _session()
        with self.assertRaises(core.RpcError) as ctx:
            core.dispatch(s, "tools/call",
                          {"name": "no_such_tool", "arguments": {}})
        self.assertEqual(ctx.exception.code, core.INVALID_PARAMS)

    def test_disabled_tool_by_env(self):
        s = _session()
        with mock.patch.dict(os.environ,
                             {"TERMUX_NATIVE_MCP_TOOLS": "run,ls,-history"},
                             clear=False):
            with self.assertRaises(core.RpcError) as ctx:
                core.dispatch(s, "tools/call",
                              {"name": "history", "arguments": {}})
            self.assertIn("disabled", str(ctx.exception))


class RiskGateTests(unittest.TestCase):

    def test_blocked_command_is_error(self):
        s = _session()
        res = _dispatch(s, "tools/call",
                        {"name": "run",
                         "arguments": {"cmd": "rm -rf /"}})
        self.assertTrue(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("Blocked", text)

        res2 = _dispatch(s, "tools/call",
                         {"name": "run",
                          "arguments": {"cmd": "rm -rf /",
                                        "confirmed": True}})
        self.assertTrue(res2.get("isError"))

    def test_warning_requires_confirmation(self):
        s = _session()
        res = _dispatch(s, "tools/call",
                        {"name": "run",
                         "arguments": {"cmd": "rm -rf deleteme"}})
        self.assertFalse(res.get("isError"))
        self.assertIn("confirmed: true", res["content"][0]["text"])

    def test_missing_cmd(self):
        s = _session()
        res = _dispatch(s, "tools/call",
                        {"name": "run", "arguments": {}})
        self.assertTrue(res.get("isError"))

    def test_cd_without_chain_is_safe_noop(self):
        s = _session()
        res = _dispatch(s, "tools/call",
                        {"name": "run", "arguments": {"cmd": "cd"}})
        self.assertFalse(res.get("isError"))
        self.assertEqual(s.cwd, os.environ.get(
            "HOME", "/data/data/com.termux/files/home"))

    def test_cd_bad_dir_is_error(self):
        s = _session()
        res = _dispatch(s, "tools/call",
                        {"name": "run",
                         "arguments": {"cmd": "cd /no/such/dir/9f3a2"}})
        self.assertTrue(res.get("isError"))
        self.assertIn("Directory not found", res["content"][0]["text"])

    def test_cancel_idle(self):
        s = _session()
        res = _dispatch(s, "tools/call", {"name": "cancel"})
        self.assertIn("Nothing running", res["content"][0]["text"])


class SessionRegistryTests(unittest.TestCase):
    def test_create_get_drop(self):
        s = core.create_session("http")
        self.assertIs(core.get_session(s.sid), s)
        core.drop_session(s.sid)
        self.assertIsNone(core.get_session(s.sid))
        self.assertTrue(s.closed.is_set())

    def test_expiry(self):
        s = core.create_session("http")
        s.last_used = 0
        self.assertIsNone(core.get_session(s.sid))
        self.assertIsNone(core.get_session(s.sid, touch=False))


class StdioLoopTests(unittest.TestCase):
    def _run_stdio(self, lines):
        inp = io.StringIO("\n".join(lines) + "\n")
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            from termux_mcp import mcp_transport_stdio as stdio
            stdio.serve_stdio(inp=inp, out=out)
        msgs = [json.loads(l) for l in out.getvalue().splitlines()]
        return msgs

    def test_initialize_and_list(self):
        msgs = self._run_stdio([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["id"], 1)
        self.assertEqual(msgs[0]["result"]["serverInfo"]["name"],
                         "termux-native-mcp")
        names = [t["name"] for t in msgs[1]["result"]["tools"]]
        self.assertIn("run", names)

    def test_notification_no_reply(self):
        msgs = self._run_stdio([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "method":
                        "notifications/initialized"}),
        ])
        self.assertEqual(len(msgs), 1)

    def test_bad_json(self):
        msgs = self._run_stdio(["{not json"])
        self.assertEqual(msgs[0]["error"]["code"], core.PARSE_ERROR)

    def test_unknown_method_error(self):
        msgs = self._run_stdio([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nope"}),
        ])
        self.assertEqual(msgs[1]["error"]["code"], core.METHOD_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
