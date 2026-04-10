import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .shell import execute_streaming, get_current_dir

logger = logging.getLogger(__name__)


class MCPHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args) -> None:
        logger.debug(fmt, *args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/ping":
            self._json_response(200, {"status": "ok", "cwd": get_current_dir()})
            return

        self._json_response(404, {"error": "Not found"})

    def do_POST(self) -> None: 
        path = urlparse(self.path).path
        data = self._read_json()

        if path == "/run":
            cmd = data.get("cmd", "").strip()
            if not cmd:
                self._json_response(400, {"error": "Missing 'cmd' field"})
                return
            execute_streaming(self, cmd)
            return

        self._json_response(404, {"error": "Not found"})
