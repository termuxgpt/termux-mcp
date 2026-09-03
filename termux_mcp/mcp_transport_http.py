import hmac
import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse


from . import mcp_config as cfg
from . import mcp_core as core


logger = logging.getLogger(__name__)


MAX_BODY = 5 * 1024 * 1024


MAX_PROGRESS_EVENTS = 200


def _auth_ok(token: str) -> bool:
    expected = cfg.native_auth_token()
    if not expected:
        return True
    if not token:
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def _bearer_from(headers) -> str:
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _origin_allowed(host: str, origin_header: str) -> bool:
    if not origin_header:
        return True
    allow = cfg.origin_allowlist()
    if origin_header in allow:
        return True
    if not allow:
        return host in ("127.0.0.1", "localhost", "::1")
    return False


def _envelope_id(msg) -> object:
    return msg.get("id") if isinstance(msg, dict) else None


class MCPRequestHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    endpoint = "/mcp"

    def log_message(self, format, *args) -> None:
        logger.debug("[MCP-HTTP] " + format, *args)

    def _send_body(self, status: int, data: bytes,
                   content_type: str = "application/json",
                   extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, obj: dict,
                   extra: Optional[dict] = None) -> None:
        self._send_body(status, json.dumps(obj).encode(), extra=extra)

    def _send_chunk(self, data: bytes) -> bool:
        try:
            self.wfile.write(hex(len(data))[2:].encode() + b"\r\n"
                             + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _finish_chunks(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def _guard(self) -> bool:
        host = self.headers.get("Host", "127.0.0.1").split(":")[0]
        if not _origin_allowed(host, self.headers.get("Origin", "")):
            self._send_json(403, {"error": "Origin not allowed"})
            return False
        if not _auth_ok(_bearer_from(self.headers)):
            self._send_body(401, b'{"error": "Unauthorized"}',
                            extra={"WWW-Authenticate": "Bearer"})
            return False
        path = urlparse(self.path).path
        if path != self.endpoint:
            self._send_json(404, {"error": "Not found"})
            return False
        return True

    def _read_jsonrpc(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return {"_error": core.INVALID_REQUEST,
                    "message": "Payload too large"}
        try:
            raw = self.rfile.read(length)
            if not raw:
                return {}
            msg = json.loads(raw.decode("utf-8"))
            return msg if isinstance(msg, dict) else {"_bad": True}
        except Exception:
            return {"_bad": True}

    def do_OPTIONS(self) -> None:

        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if not self._guard():
            return
        msg = self._read_jsonrpc()
        if "_bad" in msg:
            self._send_json(200, self._err(None, core.PARSE_ERROR,
                                           "Parse error"))
            return
        if "_error" in msg:
            code = msg["_error"]
            if not isinstance(code, int):
                code = core.INVALID_REQUEST
            self._send_json(200, self._err(None, code,
                                           msg.get("message",
                                                   "Invalid request")))
            return

        method = msg.get("method")
        req_id = _envelope_id(msg)
        params = msg.get("params") or {}
        is_request = "id" in msg

        sid = self.headers.get("Mcp-Session-Id", "")
        session = None
        if method != "initialize":
            if not sid:
                self._send_json(400, self._err(req_id, core.INVALID_REQUEST,
                                               "Missing Mcp-Session-Id"))
                return
            session = core.get_session(sid)
            if session is None:
                self._send_json(404, self._err(req_id, core.INVALID_REQUEST,
                                               "Session expired"))
                return
        else:
            session = core.create_session("http")
            sid = session.sid

        if not is_request:

            if method in ("notifications/initialized",
                          "notifications/cancelled"):
                self._run_dispatch(session, method, params, None)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        wants_stream = "text/event-stream" in self.headers.get(
            "Accept", "")
        if wants_stream and method == "tools/call":
            self._stream_call(session, req_id, method, params)
            return

        try:
            result = self._run_dispatch(session, method, params, None)
        except core.RpcError as e:
            self._send_json(200, self._err(req_id, e.code, str(e)))
            return
        except Exception as e:
            logger.exception("MCP dispatch failed")
            self._send_json(200, self._err(req_id, core.INTERNAL_ERROR,
                                           f"Internal error: {e}"))
            return
        if result is None:
            self._send_json(200, self._result(req_id, {}))
            return
        extra = {"Mcp-Session-Id": sid} if method == "initialize" else None
        self._send_json(200, self._result(req_id, result), extra=extra)

    def _stream_call(self, session, req_id, method, params) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        events = queue.Queue(maxsize=256)
        sent = [0]

        def emit(obj: dict) -> None:
            if sent[0] >= MAX_PROGRESS_EVENTS:
                return
            sent[0] += 1
            try:
                events.put_nowait(("data", json.dumps(obj)))
            except queue.Full:
                pass

        def worker() -> None:
            try:
                result = self._run_dispatch(session, method, params, emit)
                if result is None:
                    result = {}
                events.put(("data", json.dumps(self._result(req_id, result))))
            except core.RpcError as e:
                events.put(("data", json.dumps(self._err(req_id, e.code,
                                                         str(e)))))
            except Exception as e:
                logger.exception("MCP streaming call failed")
                events.put(("data", json.dumps(self._err(
                    req_id, core.INTERNAL_ERROR, f"Internal error: {e}"))))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

        try:
            while True:
                try:
                    kind, payload = events.get(timeout=15)
                except queue.Empty:
                    if not self._send_chunk(b": keepalive\n\n"):
                        break
                    continue
                if kind == "done":
                    break
                if not self._send_chunk(f"data: {payload}\n\n".encode()):
                    break
        finally:
            self._finish_chunks()

    def _run_dispatch(self, session, method, params, on_progress):
        result = core.dispatch(session, method, params,
                               on_progress=on_progress)
        if method == "initialize":
            return result
        if method == "notifications/cancelled":

            session.killed.set()
            session.kill_active()
            return None
        return result

    def do_GET(self) -> None:
        if not self._guard():
            return
        if "text/event-stream" not in self.headers.get("Accept", ""):
            self._send_json(405, {"error": "Accept: text/event-stream "
                                           "required for GET"})
            return
        sid = self.headers.get("Mcp-Session-Id", "")
        session = core.get_session(sid) if sid else None
        if session is None:
            self._send_json(404, self._err(None, core.INVALID_REQUEST,
                                           "Session required for SSE stream"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while not session.closed.is_set():
                if not self._send_chunk(b": keepalive\n\n"):
                    break
                session.closed.wait(15)
        finally:
            self._finish_chunks()
            try:
                self.connection.close()
            except Exception:
                pass

    def do_DELETE(self) -> None:
        if not self._guard():
            return
        sid = self.headers.get("Mcp-Session-Id", "")
        if not sid or core.get_session(sid, touch=False) is None:
            self._send_json(404, {"error": "Session not found"})
            return
        core.drop_session(sid)
        self._send_json(200, {"ok": True})

    def _result(self, req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(self, req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}
