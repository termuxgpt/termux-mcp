import http.client
import json
import os
import sys
import threading
import unittest
from typing import Optional
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from termux_mcp import mcp_core as core
from termux_mcp.mcp_server import MCPHTTPServer
from termux_mcp.mcp_transport_http import MCPRequestHandler


TOKEN = "x" * 24


class HTTPTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MCPHTTPServer(("127.0.0.1", 0), MCPRequestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self):
        core._SESSIONS.clear()

    def _post(self, body: dict, sid: Optional[str] = None, headers=None,
              accept=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if sid:
            hdrs["Mcp-Session-Id"] = sid
        if accept:
            hdrs["Accept"] = accept
        conn.request("POST", "/mcp", body=json.dumps(body), headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_initialize_issues_session(self):
        resp, data = self._post({"jsonrpc": "2.0", "id": 1,
                                 "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(resp.status, 200)
        sid = resp.getheader("Mcp-Session-Id")
        self.assertTrue(sid)
        msg = json.loads(data)
        self.assertEqual(msg["id"], 1)
        self.assertEqual(msg["result"]["serverInfo"]["name"],
                         "termux-native-mcp")
        self.assertEqual(msg["result"]["protocolVersion"], "2025-06-18")

    def test_flow_initialize_list_ping_delete(self):
        _, data = self._post({"jsonrpc": "2.0", "id": 1,
                              "method": "initialize"})
        self.assertEqual(json.loads(data)["result"]["serverInfo"]["name"],
                         "termux-native-mcp")
        sid = self._last_session()
        resp, data = self._post({"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list"}, sid=sid)
        self.assertEqual(resp.status, 200)
        names = [t["name"] for t in json.loads(data)["result"]["tools"]]
        self.assertIn("run", names)
        resp, data = self._post({"jsonrpc": "2.0", "id": 3,
                                 "method": "ping"}, sid=sid)
        self.assertEqual(json.loads(data)["result"], {})
        resp, _ = self._delete(sid)
        self.assertEqual(resp.status, 200)

        resp, _ = self._post({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                             sid=sid)
        self.assertEqual(resp.status, 404)

    def _last_session(self):
        with core._SESSIONS_LOCK:
            return next(iter(core._SESSIONS))

    def _delete(self, sid):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_session_header_required(self):
        resp, data = self._post({"jsonrpc": "2.0", "id": 1,
                                 "method": "ping"})
        self.assertEqual(resp.status, 400)
        resp, data = self._post({"jsonrpc": "2.0", "id": 1,
                                 "method": "ping"}, sid="deadbeef" * 4)
        self.assertEqual(resp.status, 404)

    def test_notification_returns_202(self):
        _, _ = self._post({"jsonrpc": "2.0", "id": 1,
                           "method": "initialize"})
        sid = self._last_session()
        resp, data = self._post({"jsonrpc": "2.0",
                                 "method": "notifications/initialized"},
                                sid=sid)
        self.assertEqual(resp.status, 202)
        self.assertEqual(data, b"")

    def test_unknown_method_jsonrpc_error(self):
        _, _ = self._post({"jsonrpc": "2.0", "id": 1,
                           "method": "initialize"})
        sid = self._last_session()
        resp, data = self._post({"jsonrpc": "2.0", "id": 2,
                                 "method": "no/such"}, sid=sid)
        self.assertEqual(resp.status, 200)
        msg = json.loads(data)
        self.assertEqual(msg["error"]["code"], core.METHOD_NOT_FOUND)

    def test_unknown_tool_error(self):
        _, _ = self._post({"jsonrpc": "2.0", "id": 1,
                           "method": "initialize"})
        sid = self._last_session()
        resp, data = self._post({"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/call",
                                 "params": {"name": "ghost_tool",
                                            "arguments": {}}}, sid=sid)
        msg = json.loads(data)
        self.assertEqual(msg["error"]["code"], core.INVALID_PARAMS)

    def test_parse_error(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp", body=b"{oops",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["error"]["code"], core.PARSE_ERROR)

    def test_auth_required(self):
        with mock.patch.dict(os.environ,
                             {"TERMUX_NATIVE_MCP_AUTH_TOKEN": TOKEN},
                             clear=False):
            conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                              timeout=10)
            conn.request("POST", "/mcp",
                         body=json.dumps({"jsonrpc": "2.0", "id": 1,
                                          "method": "initialize"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 401)

            resp, data = self._post({"jsonrpc": "2.0", "id": 1,
                                     "method": "initialize"},
                                    headers={"Authorization":
                                             f"Bearer {TOKEN}"})
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.getheader("Mcp-Session-Id"))

    def test_sse_streamed_call(self):
        _, _ = self._post({"jsonrpc": "2.0", "id": 1,
                           "method": "initialize"})
        sid = self._last_session()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp",
                     body=json.dumps({"jsonrpc": "2.0", "id": 7,
                                      "method": "tools/call",
                                      "params": {"name": "cancel",
                                                 "arguments": {}}}),
                     headers={"Content-Type": "application/json",
                              "Accept": "text/event-stream",
                              "Mcp-Session-Id": sid})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"),
                         "text/event-stream")
        data_lines = [l[6:] for l in body.splitlines()
                      if l.startswith("data: ")]
        self.assertEqual(len(data_lines), 1)
        msg = json.loads(data_lines[0])
        self.assertEqual(msg["id"], 7)
        self.assertIn("result", msg)
        self.assertEqual(msg["result"]["content"][0]["text"],
                         "Nothing running.")

    def test_get_requires_sse_accept(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 405)

    def test_get_stream_lifecycle(self):
        _, _ = self._post({"jsonrpc": "2.0", "id": 1,
                           "method": "initialize"})
        sid = self._last_session()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/mcp",
                     headers={"Accept": "text/event-stream",
                              "Mcp-Session-Id": sid})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"),
                         "text/event-stream")
        first = resp.read1()
        self.assertIn(b": keepalive", first)

        del_resp, _ = self._delete(sid)
        self.assertEqual(del_resp.status, 200)

        self.assertEqual(resp.read(), b"")
        conn.close()


if __name__ == "__main__":
    unittest.main()
