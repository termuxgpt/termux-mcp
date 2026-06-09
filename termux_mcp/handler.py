import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .config import AUTH_TOKEN, HOME, REQUIRE_AUTH
from .security import get_risk_assessment
from .shell import (
    cancel_active,
    execute_streaming,
    get_active_pid,
    get_current_dir,
    set_current_dir,
)

logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MB


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("[HTTP] " + fmt, *args)

    def _log(self, msg: str) -> None:
        logger.info(f"[MCP] {msg}")

    def _authenticate(self) -> bool:
        """Check Bearer token if auth is required. Returns True if allowed."""
        if not REQUIRE_AUTH:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return _constant_time_compare(token, AUTH_TOKEN)
        return False

    def _send_unauthorized(self) -> None:
        body = json.dumps({"error": "Unauthorized"}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_SIZE:
                return {"_error": "Payload too large"}
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")
            self._log(f"Body: {raw}")
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

    # ── GET ─────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self._log(f"GET {path}")

        if path == "/ping":
            self._json_response(200, {
                "status": "ok",
                "cwd": get_current_dir(),
            })
            return

        if path == "/env":
            self._json_response(200, {
                "cwd": get_current_dir(),
                "home": HOME,
                "pid": os.getpid(),
                "active_command_pid": get_active_pid(),
            })
            return

        self._json_response(404, {"error": "Not found"})

    # ── POST ────────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        self._log(f"POST {path}")

        # Auth gate for all POST endpoints
        if not self._authenticate():
            self._send_unauthorized()
            return

        data = self._read_json()
        if "_error" in data:
            self._json_response(413, {"error": data["_error"]})
            return

        if path == "/run":
            self._handle_run(data)
            return

        if path == "/ls":
            self._handle_ls(data)
            return

        if path == "/read":
            self._handle_read(data)
            return

        if path == "/cancel":
            ok = cancel_active()
            self._json_response(200, {"cancelled": ok})
            return

        if path == "/write":
            self._handle_write(data)
            return

        if path == "/mkdir":
            self._handle_mkdir(data)
            return

        if path == "/delete":
            self._handle_delete(data)
            return

        if path == "/search":
            self._handle_search(data)
            return

        self._json_response(404, {"error": "Not found"})

    # ── Handlers ────────────────────────────────────────────────────────────

    def _handle_run(self, data: dict) -> None:
        cmd = data.get("cmd", "").strip()
        if not cmd:
            self._json_response(400, {"error": "Missing 'cmd'"})
            return

        # Security check
        risk = get_risk_assessment(cmd)
        if risk["blocked"]:
            self._json_response(403, {
                "error": risk["message"],
                "risk_level": risk["risk_level"],
                "blocked": True,
            })
            return

        if risk["requires_confirmation"]:
            # Return the risk assessment — client must re-send with confirmed: true
            if not data.get("confirmed"):
                self._json_response(200, {
                    "status": "confirmation_required",
                    "command": cmd,
                    "risk_level": risk["risk_level"],
                    "message": risk["message"],
                    "requires_confirmation": True,
                })
                return

        self._log(f"Executing: {cmd}")
        execute_streaming(self, cmd)

    def _handle_ls(self, data: dict) -> None:
        path = (data.get("path") or ".").strip()
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'ls -la "{path}" 2>/dev/null || echo "Cannot access: {path}"')

    def _handle_read(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'head -n 500 "{path}" 2>/dev/null || echo "Cannot read: {path}"')

    def _handle_write(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        content = (data.get("content") or "").strip()
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        # Write via heredoc to handle special characters safely
        safe_content = content.replace("'", "'\"'\"'")
        execute_streaming(
            self,
            f'mkdir -p "$(dirname "{path}")" 2>/dev/null; cat > "{path}" << \'EOF\'\n{content}\nEOF\necho "Written: {path}"'
        )

    def _handle_mkdir(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'mkdir -p "{path}" && echo "Created: {path}"')

    def _handle_delete(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        recursive = data.get("recursive", False)
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        # Always require confirmation for delete
        if not data.get("confirmed"):
            self._json_response(200, {
                "status": "confirmation_required",
                "command": f"rm {'-rf' if recursive else ''} {path}",
                "risk_level": "warning",
                "message": f"Delete: {path}",
                "requires_confirmation": True,
            })
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        flags = "-rf" if recursive else ""
        execute_streaming(self, f'rm {flags} "{path}" 2>/dev/null && echo "Deleted: {path}" || echo "Failed to delete: {path}"')

    def _handle_search(self, data: dict) -> None:
        path = (data.get("path") or ".").strip()
        pattern = (data.get("pattern") or "*").strip()
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'find "{path}" -name "{pattern}" -type f 2>/dev/null | head -n 30')

    def _is_safe_path(self, path: str) -> bool:
        """Prevent operations on sensitive system files."""
        blocked = ("/dev/", "/proc/", "/sys/")
        for prefix in blocked:
            if path.startswith(prefix):
                return False
        return True
