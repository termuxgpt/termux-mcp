import json
import os
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from termux_mcp import mcp_bridge as bridge
from termux_mcp import mcp_core as core


class DechunkTests(unittest.TestCase):
    def test_single_frame(self):
        self.assertEqual(bridge.dechunk(b"4\r\ntest\r\n0\r\n\r\n"), b"test")

    def test_multiple_frames(self):
        data = b"4\r\ntest\r\n6\r\n world\r\n0\r\n\r\n"
        self.assertEqual(bridge.dechunk(data), b"test world")

    def test_binary_safe(self):
        payload = bytes(range(256))
        frame = hex(256)[2:].encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n"
        self.assertEqual(bridge.dechunk(frame), payload)


class AnalyzeOutputTests(unittest.TestCase):
    def test_rest_success_marker(self):
        meta = bridge.analyze_output("hello\n✅ Done\n")
        self.assertFalse(meta["error"])
        self.assertIsNone(meta["exit_code"])
        self.assertEqual(meta["text"], "hello")

    def test_rest_failure_marker(self):
        meta = bridge.analyze_output("boom\n❌ Exit code: 2\n")
        self.assertEqual(meta["exit_code"], 2)
        self.assertEqual(meta["text"], "boom")

    def test_ws_tags(self):
        ok = bridge.analyze_output("a\nDone\n")
        self.assertIsNone(ok["exit_code"])
        err = bridge.analyze_output("a\nExit: 9\n")
        self.assertEqual(err["exit_code"], 9)
        det = bridge.analyze_output("a\nExit: (process detached)\n")
        self.assertIsNone(det["exit_code"])

    def test_cancelled(self):
        meta = bridge.analyze_output("partial\nCancelled\n")
        self.assertTrue(meta["cancelled"])
        self.assertEqual(meta["text"], "partial")

    def test_timeout(self):
        meta = bridge.analyze_output("stuck\n⏱️ Timed out after 5s\n")
        self.assertTrue(meta["timed_out"])

    def test_truncation_detected_and_kept(self):
        text = "a\n" + bridge.TRUNCATION_MARKER.strip() + "\nrest\n✅ Done\n"
        meta = bridge.analyze_output(text)
        self.assertTrue(meta["truncated"])
        self.assertIn("Output truncated", meta["text"])


class VirtualHandlerTests(unittest.TestCase):
    def test_json_capture(self):
        vh = bridge.VirtualHandler()
        body = json.dumps({"status": "ok", "cwd": "/home"}).encode()
        vh.send_response(200)
        vh.send_header("Content-Type", "application/json")
        vh.send_header("Content-Length", str(len(body)))
        vh.end_headers()
        vh.wfile.write(body)
        self.assertEqual(vh.status, 200)
        self.assertEqual(vh.parsed_json(), {"status": "ok", "cwd": "/home"})

    def test_chunked_capture(self):
        vh = bridge.VirtualHandler()
        vh.send_response(200)
        vh.send_header("Content-Type", "text/plain")
        vh.send_header("Transfer-Encoding", "chunked")
        vh.end_headers()
        chunk = b"hi\n"
        vh.wfile.write(hex(len(chunk))[2:].encode() + b"\r\n"
                       + chunk + b"\r\n")
        vh.wfile.write(b"0\r\n\r\n")
        self.assertEqual(vh.decoded_text(), "hi\n")

    def test_decode_json_confirmation(self):
        vh = bridge.VirtualHandler()
        body = json.dumps({"status": "confirmation_required",
                           "risk_level": "warning",
                           "requires_confirmation": True}).encode()
        vh.send_response(200)
        vh.send_header("Content-Type", "application/json")
        vh.send_header("Content-Length", str(len(body)))
        vh.end_headers()
        vh.wfile.write(body)
        res = bridge.decode_virtual(vh)
        self.assertFalse(res["is_error"])
        self.assertIn("confirmed: true", res["text"])

    def test_decode_json_error(self):
        vh = bridge.VirtualHandler()
        body = json.dumps({"error": "Blocked dangerous command: x",
                           "blocked": True}).encode()
        vh.send_response(403)
        vh.send_header("Content-Type", "application/json")
        vh.send_header("Content-Length", str(len(body)))
        vh.end_headers()
        vh.wfile.write(body)
        res = bridge.decode_virtual(vh)
        self.assertTrue(res["is_error"])

    def test_decode_stream_markers(self):
        vh = bridge.VirtualHandler()
        vh.send_response(200)
        vh.send_header("Content-Type", "text/plain")
        vh.send_header("Transfer-Encoding", "chunked")
        vh.end_headers()
        chunk = b"all good\n\n\xe2\x9c\x85 Done\n"
        vh.wfile.write(hex(len(chunk))[2:].encode() + b"\r\n"
                       + chunk + b"\r\n")
        vh.wfile.write(b"0\r\n\r\n")
        res = bridge.decode_virtual(vh)
        self.assertFalse(res["is_error"])
        self.assertIn("all good", res["text"])


class RegistryTests(unittest.TestCase):
    def test_names_unique_and_curated(self):
        tools = bridge.build_mcp_tool_list(core.NATIVE_TOOL_DEFS)
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreater(len(names), 50)
        self.assertLess(len(names), 70)
        for alias in ("camera", "wifi", "sms", "tts", "ocr"):
            self.assertNotIn(alias, names)

    def test_schemas_valid(self):
        tools = bridge.build_mcp_tool_list(core.NATIVE_TOOL_DEFS)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            props = t["inputSchema"].get("properties", {})
            for req in t["inputSchema"].get("required", []):
                self.assertIn(req, props, t["name"])
            self.assertTrue(t["description"])

    def test_every_bridge_tool_routable(self):
        for name in bridge.bridge_tool_names():
            self.assertIsNotNone(bridge.route_callable(name), name)

        ghost = bridge._bound_method(bridge._INSTANCE_ROUTES["ls"])
        self.assertTrue(callable(ghost))

    def test_native_overrides_catalog(self):
        tools = bridge.build_mcp_tool_list(core.NATIVE_TOOL_DEFS)
        runs = [t for t in tools if t["name"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("confirmed", runs[0]["inputSchema"]["properties"])

    def test_extras_included(self):
        tools = bridge.build_mcp_tool_list(core.NATIVE_TOOL_DEFS)
        names = [t["name"] for t in tools]
        self.assertIn("text_extract", names)
        self.assertIsNotNone(bridge.route_callable("text_extract"))

    def test_filters(self):
        tools = bridge.build_mcp_tool_list(
            core.NATIVE_TOOL_DEFS, (["run", "ls"], []))
        names = [t["name"] for t in tools]
        self.assertEqual(sorted(names), ["ls", "run"])
        tools = bridge.build_mcp_tool_list(
            core.NATIVE_TOOL_DEFS, ([], ["history", "history_save"]))
        names = [t["name"] for t in tools]
        self.assertNotIn("history", names)

    def test_env_filter_applied(self):
        with mock.patch.dict(os.environ,
                             {"TERMUX_NATIVE_MCP_TOOLS": "run,ls,-history"},
                             clear=False):
            tools = core.tool_list()
            names = [t["name"] for t in tools["tools"]]
            self.assertIn("run", names)
            self.assertIn("ls", names)
            self.assertNotIn("history", names)


if __name__ == "__main__":
    unittest.main()
