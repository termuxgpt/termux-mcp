import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .shell import execute_streaming, get_current_dir

logger = logging.getLogger(__name__)


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("[HTTP] " + fmt, *args)

    def _log(self, msg: str):
        logger.info(f"[MCP] {msg}")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")
            self._log(f"Request body: {raw}")
            if not raw:
                return {}
            return json.loads(raw)
        except Exception as e:
            self._log(f"JSON read error: {e}")
            return {}

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self._log(f"GET {path}")

        if path == "/ping":
            self._json_response(200, {
                "status": "ok",
                "cwd": get_current_dir(),
            })
            return

        self._json_response(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        self._log(f"POST {path}")

        data = self._read_json()
        self._log(f"Parsed JSON: {data}")

        if path == "/run":
            cmd = data.get("cmd", "").strip()
            if not cmd:
                self._json_response(400, {"error": "Missing 'cmd'"})
                return
            self._log(f"Executing: {cmd}")
            execute_streaming(self, cmd)
            return

        self._json_response(404, {"error": "Not found"})
