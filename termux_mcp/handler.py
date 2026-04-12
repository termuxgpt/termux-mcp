import json
import logging
import subprocess
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .shell import get_current_dir

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
                "cwd": get_current_dir()
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
            self._handle_stream(cmd)
            return

        self._json_response(404, {"error": "Not found"})

    def _handle_stream(self, cmd: str):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True
            )

            for line in iter(process.stdout.readline, ''):
                if not line:
                    break

                self._log(f"OUT: {line.strip()}")
                self._write(line)

            process.wait()

            self._log(f"Command finished with code {process.returncode}")

        except Exception as e:
            error_msg = f"ERROR: {e}\n"
            self._log(error_msg)
            self._write(error_msg)

    def _write(self, text: str):
        try:
            self.wfile.write(text.encode("utf-8", errors="ignore"))
            self.wfile.flush()
        except BrokenPipeError:
            self._log("Client disconnected")
        except Exception as e:
            self._log(f"Write error: {e}")
