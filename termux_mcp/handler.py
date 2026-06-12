import base64
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .config import AUTH_TOKEN, HOME, REQUIRE_AUTH
from .handlers.ai_power import (
    handle_smart_install, handle_permission_fix, handle_profile,
    handle_error_explain, handle_ssh_wizard, handle_service_guard,
    handle_history_insight, handle_optimize, handle_quick_cmd,
    handle_port_manage, handle_migrate, handle_tutorial,
)
from .handlers.terminal import (
    handle_diagnose, handle_pkg_smart, handle_explain, handle_dev_env,
    handle_review, handle_log_analyze, handle_script_gen, handle_deps_tree,
    handle_storage_audit, handle_config_fix, handle_git_smart, handle_regex,
    handle_db_design, handle_backup, handle_restore,
)
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


def _shell_quote(s: str) -> str:
    """Wrap in single quotes, properly escaping embedded single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


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

        if path == "/screenshot":
            self._handle_screenshot(data)
            return
        if path == "/camera-photo":
            self._handle_camera_photo(data)
            return
        if path == "/camera-info":
            self._handle_camera_info(data)
            return
        if path == "/clipboard-get":
            self._handle_clipboard_get(data)
            return
        if path == "/clipboard-set":
            self._handle_clipboard_set(data)
            return
        if path == "/notify":
            self._handle_notify(data)
            return
        if path == "/notify-remove":
            self._handle_notify_remove(data)
            return
        if path == "/share":
            self._handle_share(data)
            return
        if path == "/open-url":
            self._handle_open_url(data)
            return
        if path == "/download":
            self._handle_download(data)
            return
        if path == "/battery":
            self._handle_battery(data)
            return
        if path == "/wifi-info":
            self._handle_wifi_info(data)
            return
        if path == "/wifi-scan":
            self._handle_wifi_scan(data)
            return
        if path == "/location":
            self._handle_location(data)
            return
        if path == "/contacts":
            self._handle_contacts(data)
            return
        if path == "/sms-send":
            self._handle_sms_send(data)
            return
        if path == "/sms-inbox":
            self._handle_sms_inbox(data)
            return
        if path == "/list-apps":
            self._handle_list_apps(data)
            return
        if path == "/vibrate":
            self._handle_vibrate(data)
            return
        if path == "/tts-speak":
            self._handle_tts_speak(data)
            return
        if path == "/torch":
            self._handle_torch(data)
            return
        if path == "/wallpaper":
            self._handle_wallpaper(data)
            return
        if path == "/toast":
            self._handle_toast(data)
            return
        if path == "/dialog":
            self._handle_dialog(data)
            return
        if path == "/brightness":
            self._handle_brightness(data)
            return
        if path == "/volume":
            self._handle_volume(data)
            return
        if path == "/screen-record":
            self._handle_screen_record(data)
            return
        if path == "/qrcode":
            self._handle_qrcode(data)
            return
        if path == "/fingerprint":
            self._handle_fingerprint(data)
            return
        if path == "/call":
            self._handle_call(data)
            return
        if path == "/scan-barcode":
            self._handle_scan_barcode(data)
            return
        # ── Missing termux-api ──────────────────────────────
        if path == "/sensor":
            self._handle_sensor(data)
            return
        if path == "/microphone-record":
            self._handle_microphone_record(data)
            return
        if path == "/speech-to-text":
            self._handle_speech_to_text(data)
            return
        if path == "/media-player":
            self._handle_media_player(data)
            return
        if path == "/storage-get":
            self._handle_storage_get(data)
            return
        if path == "/telephony-deviceinfo":
            self._handle_telephony_deviceinfo(data)
            return
        if path == "/telephony-cellinfo":
            self._handle_telephony_cellinfo(data)
            return
        if path == "/infrared":
            self._handle_infrared(data)
            return
        # ── Linux Tool Endpoints ────────────────────────────
        if path == "/speedtest":
            self._handle_speedtest(data)
            return
        if path == "/image-process":
            self._handle_image_process(data)
            return
        if path == "/video-process":
            self._handle_video_process(data)
            return
        if path == "/text-extract":
            self._handle_text_extract(data)
            return
        if path == "/public-ip":
            self._handle_public_ip(data)
            return
        if path == "/weather":
            self._handle_weather(data)
            return
        if path == "/translate":
            self._handle_translate(data)
            return
        if path == "/db-query":
            self._handle_db_query(data)
            return
        if path == "/web-server":
            self._handle_web_server(data)
            return
        if path == "/git-op":
            self._handle_git_op(data)
            return
        # ── Termux Power-Tools ──────────────────────────────
        if path == "/diagnose":
            handle_diagnose(self, data)
            return
        if path == "/pkg-smart":
            handle_pkg_smart(self, data)
            return
        if path == "/explain":
            handle_explain(self, data)
            return
        if path == "/dev-env":
            handle_dev_env(self, data)
            return
        if path == "/review":
            handle_review(self, data)
            return
        if path == "/log-analyze":
            handle_log_analyze(self, data)
            return
        if path == "/script-gen":
            handle_script_gen(self, data)
            return
        if path == "/deps-tree":
            handle_deps_tree(self, data)
            return
        if path == "/storage-audit":
            handle_storage_audit(self, data)
            return
        if path == "/config-fix":
            handle_config_fix(self, data)
            return
        if path == "/git-smart":
            handle_git_smart(self, data)
            return
        if path == "/regex":
            handle_regex(self, data)
            return
        if path == "/db-design":
            handle_db_design(self, data)
            return
        if path == "/backup":
            handle_backup(self, data)
            return
        if path == "/restore":
            handle_restore(self, data)
            return
        # ── AI-Native Power Features ─────────────────────────
        if path == "/smart-install":
            handle_smart_install(self, data)
            return
        if path == "/permission-fix":
            handle_permission_fix(self, data)
            return
        if path == "/profile":
            handle_profile(self, data)
            return
        if path == "/error-explain":
            handle_error_explain(self, data)
            return
        if path == "/ssh-wizard":
            handle_ssh_wizard(self, data)
            return
        if path == "/service-guard":
            handle_service_guard(self, data)
            return
        if path == "/history-insight":
            handle_history_insight(self, data)
            return
        if path == "/optimize":
            handle_optimize(self, data)
            return
        if path == "/quick-cmd":
            handle_quick_cmd(self, data)
            return
        if path == "/port-manage":
            handle_port_manage(self, data)
            return
        if path == "/migrate":
            handle_migrate(self, data)
            return
        if path == "/tutorial":
            handle_tutorial(self, data)
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
        execute_streaming(self, f'ls -la {_shell_quote(path)} 2>/dev/null || echo "Cannot access: {path}"')

    def _handle_read(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'head -n 500 {_shell_quote(path)} 2>/dev/null || echo "Cannot read: {path}"')

    def _handle_write(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        content = (data.get("content") or "")
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        # Write via base64 to avoid shell escaping issues entirely
        encoded = base64.b64encode(content.encode()).decode()
        execute_streaming(
            self,
            f'mkdir -p "$(dirname {_shell_quote(path)})" 2>/dev/null; '
            f'echo {_shell_quote(encoded)} | base64 -d > {_shell_quote(path)} && '
            f'echo "Written: {path}"'
        )

    def _handle_mkdir(self, data: dict) -> None:
        path = (data.get("path") or "").strip()
        if not path:
            self._json_response(400, {"error": "Missing 'path'"})
            return
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'mkdir -p {_shell_quote(path)} && echo "Created: {path}"')

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
        execute_streaming(self, f'rm {flags} {_shell_quote(path)} 2>/dev/null && echo "Deleted: {path}" || echo "Failed to delete: {path}"')

    def _handle_search(self, data: dict) -> None:
        path = (data.get("path") or ".").strip()
        pattern = (data.get("pattern") or "*").strip()
        if not self._is_safe_path(path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f'find {_shell_quote(path)} -name {_shell_quote(pattern)} -type f 2>/dev/null | head -n 30')

    # ── Vision & Communication ────────────────────────────────────────────

    def _handle_screenshot(self, data: dict) -> None:
        output = data.get("output", "").strip()
        if output:
            execute_streaming(self, f"termux-screenshot -o {_shell_quote(output)} 2>/dev/null || echo 'Screenshot failed'")
        else:
            execute_streaming(self, "termux-screenshot 2>/dev/null || echo 'Screenshot failed'")

    def _handle_camera_photo(self, data: dict) -> None:
        camera_id = str(data.get("camera_id", 0))
        output = data.get("output", "").strip() or "/sdcard/DCIM/termux_photo.jpg"
        execute_streaming(self, f"termux-camera-photo -c {camera_id} {_shell_quote(output)} 2>/dev/null || echo 'Camera photo failed'")

    def _handle_camera_info(self, data: dict) -> None:
        execute_streaming(self, "termux-camera-info 2>/dev/null || echo '{}'")

    def _handle_clipboard_get(self, data: dict) -> None:
        execute_streaming(self, "termux-clipboard-get 2>/dev/null || echo '(clipboard empty)'")

    def _handle_clipboard_set(self, data: dict) -> None:
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"error": "Missing 'text'"})
            return
        execute_streaming(self, f"echo {_shell_quote(text)} | termux-clipboard-set && echo 'Clipboard set' || echo 'Failed'")

    def _handle_notify(self, data: dict) -> None:
        title = data.get("title", "TermuxGPT").strip()
        content = data.get("content", "").strip()
        if not content:
            self._json_response(400, {"error": "Missing 'content'"})
            return
        priority = data.get("priority", "default").strip()
        nid = data.get("id", "").strip()
        flags = ""
        if nid:
            flags += f" --id {nid}"
        if data.get("ongoing"):
            flags += " --ongoing"
        execute_streaming(self, f"termux-notification {flags} --priority {priority} --title {_shell_quote(title)} --content {_shell_quote(content)} 2>/dev/null && echo 'Notification sent' || echo 'Notification failed'")

    def _handle_notify_remove(self, data: dict) -> None:
        nid = str(data.get("id", "")).strip()
        if not nid:
            self._json_response(400, {"error": "Missing 'id'"})
            return
        execute_streaming(self, f"termux-notification-remove {nid} 2>/dev/null && echo 'Removed' || echo 'Failed'")

    def _handle_share(self, data: dict) -> None:
        text = data.get("text", "").strip()
        file_path = data.get("file", "").strip()
        if file_path:
            if not self._is_safe_path(file_path):
                self._json_response(403, {"error": "Path not allowed"})
                return
            execute_streaming(self, f"termux-share -a send {_shell_quote(file_path)} 2>/dev/null || echo 'Share failed'")
        elif text:
            execute_streaming(self,
                f"echo {_shell_quote(text)} > /data/data/com.termux/files/usr/tmp/termux_share.txt 2>/dev/null && "
                f"termux-share -a send /data/data/com.termux/files/usr/tmp/termux_share.txt 2>/dev/null && "
                f"echo 'Share opened' || echo 'Share failed'")
        else:
            self._json_response(400, {"error": "Missing 'text' or 'file'"})

    def _handle_open_url(self, data: dict) -> None:
        url = data.get("url", "").strip()
        if not url:
            self._json_response(400, {"error": "Missing 'url'"})
            return
        execute_streaming(self, f"termux-open-url {_shell_quote(url)} 2>/dev/null && echo 'Opened: {url}' || echo 'Failed to open'")

    # ── Device Control & Info ──────────────────────────────────────────────

    def _handle_download(self, data: dict) -> None:
        url = data.get("url", "").strip()
        if not url:
            self._json_response(400, {"error": "Missing 'url'"})
            return
        desc = data.get("description", "").strip()
        title = data.get("title", "").strip()
        flags = ""
        if desc:
            flags += f" -d {_shell_quote(desc)}"
        if title:
            flags += f" -t {_shell_quote(title)}"
        execute_streaming(self, f"termux-download{flags} {_shell_quote(url)} 2>/dev/null && echo 'Download started' || echo 'Download failed'")

    def _handle_battery(self, data: dict) -> None:
        execute_streaming(self, "termux-battery-status 2>/dev/null || echo '{}'")

    def _handle_wifi_info(self, data: dict) -> None:
        execute_streaming(self, "termux-wifi-connectioninfo 2>/dev/null || echo '{}'")

    def _handle_wifi_scan(self, data: dict) -> None:
        execute_streaming(self, "termux-wifi-scaninfo 2>/dev/null || echo '[]'")

    def _handle_location(self, data: dict) -> None:
        provider = data.get("provider", "gps").strip()
        execute_streaming(self, f"termux-location -p {_shell_quote(provider)} -r last 2>/dev/null || echo '{{}}'")

    def _handle_contacts(self, data: dict) -> None:
        execute_streaming(self, "termux-contact-list 2>/dev/null || echo '[]'")

    def _handle_sms_send(self, data: dict) -> None:
        number = data.get("number", "").strip()
        text = data.get("text", "").strip()
        if not number or not text:
            self._json_response(400, {"error": "Missing 'number' or 'text'"})
            return
        execute_streaming(self, f"termux-sms-send -n {_shell_quote(number)} {_shell_quote(text)} 2>/dev/null && echo 'SMS sent' || echo 'SMS failed'")

    def _handle_sms_inbox(self, data: dict) -> None:
        limit = str(data.get("limit", 10))
        execute_streaming(self, f"termux-sms-inbox -n {limit} 2>/dev/null || echo '[]'")

    def _handle_list_apps(self, data: dict) -> None:
        execute_streaming(self, "termux-app-list 2>/dev/null || echo '{}'")

    def _handle_vibrate(self, data: dict) -> None:
        duration = str(data.get("duration_ms", 500))
        execute_streaming(self, f"termux-vibrate -d {duration} 2>/dev/null && echo 'Vibrated {duration}ms' || echo 'Vibrate failed'")

    # ── Automation & Media ─────────────────────────────────────────────────

    def _handle_tts_speak(self, data: dict) -> None:
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"error": "Missing 'text'"})
            return
        rate = str(data.get("rate", 1.0))
        pitch = str(data.get("pitch", 1.0))
        execute_streaming(self, f"termux-tts-speak --rate {rate} --pitch {pitch} {_shell_quote(text)} 2>/dev/null && echo 'Spoken' || echo 'TTS failed'")

    def _handle_torch(self, data: dict) -> None:
        state = data.get("state", "on").strip().lower()
        if state not in ("on", "off"):
            self._json_response(400, {"error": "State must be 'on' or 'off'"})
            return
        execute_streaming(self, f"termux-torch {state} 2>/dev/null && echo 'Torch {state}' || echo 'Torch failed'")

    def _handle_wallpaper(self, data: dict) -> None:
        file_path = data.get("file", "").strip()
        lockscreen = data.get("lockscreen", False)
        if file_path:
            if not self._is_safe_path(file_path):
                self._json_response(403, {"error": "Path not allowed"})
                return
        flags = "-l" if lockscreen else ""
        if file_path:
            execute_streaming(self, f"termux-wallpaper {flags} -f {_shell_quote(file_path)} 2>/dev/null && echo 'Wallpaper set' || echo 'Wallpaper failed'")
        else:
            execute_streaming(self, f"termux-wallpaper {flags} 2>/dev/null && echo 'Wallpaper set' || echo 'Wallpaper failed'")

    # ── Next-Level Viral Features ────────────────────────────────────────

    def _handle_toast(self, data: dict) -> None:
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"error": "Missing 'text'"})
            return
        short = str(data.get("short_duration", True)).lower() == "true"
        flags = " -s" if short else " -l"
        execute_streaming(self, f"termux-toast{flags} {_shell_quote(text)} 2>/dev/null && echo 'Toast shown' || echo 'Toast failed'")

    def _handle_dialog(self, data: dict) -> None:
        title = data.get("title", "TermuxGPT").strip()
        msg = data.get("message", "").strip()
        if not msg:
            self._json_response(400, {"error": "Missing 'message'"})
            return
        execute_streaming(
            self,
            f"termux-dialog confirm -t {_shell_quote(title)} -i {_shell_quote(msg)} 2>/dev/null && echo 'Dialog shown' || echo 'Dialog failed'"
        )

    def _handle_brightness(self, data: dict) -> None:
        level = str(data.get("level", ""))
        if not level:
            execute_streaming(self, "termux-brightness 2>/dev/null || echo '{}'")
        else:
            execute_streaming(self, f"termux-brightness {level} 2>/dev/null && echo 'Brightness set to {level}' || echo 'Brightness failed'")

    def _handle_volume(self, data: dict) -> None:
        stream = data.get("stream", "music").strip()
        level = str(data.get("level", ""))
        if level:
            execute_streaming(self, f"termux-volume {_shell_quote(stream)} {level} 2>/dev/null && echo 'Volume set' || echo 'Volume failed'")
        else:
            execute_streaming(self, f"termux-volume {_shell_quote(stream)} 2>/dev/null || echo 'Volume failed'")

    def _handle_screen_record(self, data: dict) -> None:
        output = data.get("output", "/sdcard/DCIM/screen_record.mp4").strip()
        action = data.get("action", "start").strip()
        if action == "stop":
            execute_streaming(self, "termux-screen-record -q 2>/dev/null && echo 'Recording stopped' || echo 'Stop failed'")
        else:
            execute_streaming(self, f"termux-screen-record -o {_shell_quote(output)} 2>/dev/null && echo 'Recording started' || echo 'Recording failed'")

    def _handle_qrcode(self, data: dict) -> None:
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"error": "Missing 'text'"})
            return
        output = data.get("output", "/sdcard/DCIM/qrcode.png").strip()
        execute_streaming(
            self,
            f"qrencode -o {_shell_quote(output)} {_shell_quote(text)} 2>/dev/null && echo 'QR code saved to {output}' || echo 'Install qrencode: pkg install qrencode'"
        )

    def _handle_fingerprint(self, data: dict) -> None:
        execute_streaming(self, "termux-fingerprint 2>/dev/null && echo 'AUTH_SUCCESS' || echo 'AUTH_FAILED'")

    def _handle_call(self, data: dict) -> None:
        number = data.get("number", "").strip()
        if not number:
            self._json_response(400, {"error": "Missing 'number'"})
            return
        execute_streaming(self, f"termux-telephony-call {_shell_quote(number)} 2>/dev/null && echo 'Calling {number}' || echo 'Call failed'")

    def _handle_scan_barcode(self, data: dict) -> None:
        camera_id = str(data.get("camera_id", 0))
        output = data.get("output", "/sdcard/DCIM/barcode_capture.jpg").strip()
        execute_streaming(
            self,
            f"termux-camera-photo -c {camera_id} {_shell_quote(output)} 2>/dev/null && "
            f"zbarimg -q {_shell_quote(output)} 2>/dev/null || echo 'Install zbar: pkg install zbar'"
        )

    # ── Missing termux-api Handlers ──────────────────────────────────────

    def _handle_sensor(self, data: dict) -> None:
        sensor_name = data.get("sensor", "").strip()
        limit = str(data.get("limit", 1))
        if sensor_name:
            execute_streaming(self, f"termux-sensor -s {_shell_quote(sensor_name)} -n {limit} 2>/dev/null || echo 'Sensor failed'")
        else:
            execute_streaming(self, "termux-sensor -l 2>/dev/null || echo 'Sensor list failed'")

    def _handle_microphone_record(self, data: dict) -> None:
        output = data.get("output", "/sdcard/DCIM/termux_recording.mp3").strip()
        limit = str(data.get("limit_seconds", 10))
        action = data.get("action", "start").strip()
        if action == "stop":
            execute_streaming(self, "termux-microphone-record -q 2>/dev/null && echo 'Recording stopped' || echo 'Stop failed'")
        else:
            execute_streaming(self, f"termux-microphone-record -l {limit} -f {_shell_quote(output)} 2>/dev/null && echo 'Recording started' || echo 'Mic failed'")

    def _handle_speech_to_text(self, data: dict) -> None:
        execute_streaming(self, "termux-speech-to-text 2>/dev/null || echo 'STT unavailable'")

    def _handle_media_player(self, data: dict) -> None:
        action = data.get("action", "info").strip()
        valid = ("play", "pause", "stop", "info", "next", "previous")
        if action not in valid:
            self._json_response(400, {"error": f"Action must be one of: {', '.join(valid)}"})
            return
        execute_streaming(self, f"termux-media-player {action} 2>/dev/null || echo 'Media player failed'")

    def _handle_storage_get(self, data: dict) -> None:
        output = data.get("output", "").strip()
        if not output:
            self._json_response(400, {"error": "Missing 'output' path"})
            return
        execute_streaming(self, f"termux-storage-get {_shell_quote(output)} 2>/dev/null && echo 'File saved to {output}' || echo 'Storage get failed'")

    def _handle_telephony_deviceinfo(self, data: dict) -> None:
        execute_streaming(self, "termux-telephony-deviceinfo 2>/dev/null || echo '{}'")

    def _handle_telephony_cellinfo(self, data: dict) -> None:
        execute_streaming(self, "termux-telephony-cellinfo 2>/dev/null || echo '[]'")

    def _handle_infrared(self, data: dict) -> None:
        frequency = str(data.get("frequency", "")).strip()
        pattern = data.get("pattern", "").strip()
        if not frequency or not pattern:
            self._json_response(400, {"error": "Missing 'frequency' or 'pattern'"})
            return
        execute_streaming(self,
            f"termux-infrared-transmit -f {frequency} {_shell_quote(pattern)} 2>/dev/null && "
            f"echo 'IR transmitted' || echo 'IR failed - install termux-infrared'")

    # ── Linux Tool Handlers ──────────────────────────────────────────────

    def _handle_speedtest(self, data: dict) -> None:
        execute_streaming(self, "speedtest-cli --simple 2>/dev/null || echo 'Install: pkg install speedtest-cli'")

    def _handle_image_process(self, data: dict) -> None:
        action = data.get("action", "info").strip()
        input_file = data.get("input", "").strip()
        output_file = data.get("output", "").strip()
        if not input_file:
            self._json_response(400, {"error": "Missing 'input' path"})
            return
        if not self._is_safe_path(input_file):
            self._json_response(403, {"error": "Path not allowed"})
            return
        safe_in = _shell_quote(input_file)
        safe_out = _shell_quote(output_file) if output_file else ""

        if action == "info":
            execute_streaming(self, f"identify -verbose {safe_in} 2>/dev/null || echo 'Install: pkg install imagemagick'")
        elif action == "resize" and output_file:
            w = data.get("width", 800)
            h = data.get("height", 600)
            execute_streaming(self, f"convert {safe_in} -resize {w}x{h}! {safe_out} 2>/dev/null && echo 'Resized to {w}x{h}' || echo 'Failed'")
        elif action == "crop" and output_file:
            w = data.get("width", 100)
            h = data.get("height", 100)
            x = data.get("x", 0)
            y = data.get("y", 0)
            execute_streaming(self, f"convert {safe_in} -crop {w}x{h}+{x}+{y} {safe_out} 2>/dev/null && echo 'Cropped' || echo 'Failed'")
        elif action == "rotate" and output_file:
            degrees = data.get("degrees", 90)
            execute_streaming(self, f"convert {safe_in} -rotate {degrees} {safe_out} 2>/dev/null && echo 'Rotated {degrees}°' || echo 'Failed'")
        else:
            self._json_response(400, {"error": "Unknown action or missing output"})

    def _handle_video_process(self, data: dict) -> None:
        action = data.get("action", "info").strip()
        input_file = data.get("input", "").strip()
        output_file = data.get("output", "").strip()
        if not input_file:
            self._json_response(400, {"error": "Missing 'input' path"})
            return
        if not self._is_safe_path(input_file):
            self._json_response(403, {"error": "Path not allowed"})
            return
        safe_in = _shell_quote(input_file)
        safe_out = _shell_quote(output_file) if output_file else ""

        if action == "info":
            execute_streaming(self, f"ffprobe -v quiet -print_format json -show_format -show_streams {safe_in} 2>/dev/null || echo 'Install: pkg install ffmpeg'")
        elif action == "compress" and output_file:
            crf = data.get("crf", 28)
            execute_streaming(self, f"ffmpeg -i {safe_in} -vcodec libx264 -crf {crf} {safe_out} 2>&1 | tail -5 || echo 'Failed'")
        elif action == "extract-audio" and output_file:
            execute_streaming(self, f"ffmpeg -i {safe_in} -q:a 0 -map a {safe_out} 2>&1 | tail -3 || echo 'Failed'")
        elif action == "trim" and output_file:
            start = data.get("start", "00:00:00")
            duration = data.get("duration", 10)
            execute_streaming(self, f"ffmpeg -i {safe_in} -ss {start} -t {duration} -c copy {safe_out} 2>&1 | tail -3 || echo 'Failed'")
        else:
            self._json_response(400, {"error": "Unknown action or missing output"})

    def _handle_text_extract(self, data: dict) -> None:
        input_file = data.get("input", "").strip()
        lang = data.get("lang", "eng").strip()
        if not input_file:
            self._json_response(400, {"error": "Missing 'input' path"})
            return
        if not self._is_safe_path(input_file):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f"tesseract {_shell_quote(input_file)} stdout -l {lang} 2>/dev/null || echo 'Install: pkg install tesseract'")

    def _handle_public_ip(self, data: dict) -> None:
        execute_streaming(self, "curl -s https://api.ipify.org 2>/dev/null || curl -s https://ifconfig.me 2>/dev/null || echo 'No internet'")

    def _handle_weather(self, data: dict) -> None:
        city = data.get("city", "").strip()
        if city:
            execute_streaming(self, f"curl -s wttr.in/{_shell_quote(city)}?format=3 2>/dev/null || echo 'Install curl: pkg install curl'")
        else:
            execute_streaming(self, "curl -s 'wttr.in/?format=3' 2>/dev/null || echo 'Install curl: pkg install curl'")

    def _handle_translate(self, data: dict) -> None:
        text = data.get("text", "").strip()
        target = data.get("target_lang", "en").strip()
        source = data.get("source_lang", "auto").strip()
        if not text:
            self._json_response(400, {"error": "Missing 'text'"})
            return
        execute_streaming(self,
            f"curl -s \"https://translate.googleapis.com/translate_a/single?client=gtx&sl={source}&tl={target}&dt=t&q="
            f"{_shell_quote(text)}\" 2>/dev/null | python3 -c \"import sys,json; print(json.load(sys.stdin)[0][0][0])\" 2>/dev/null "
            f"|| echo 'Translation failed'")

    def _handle_db_query(self, data: dict) -> None:
        db_path = data.get("database", "").strip()
        query = data.get("query", "").strip()
        if not db_path or not query:
            self._json_response(400, {"error": "Missing 'database' or 'query'"})
            return
        if not self._is_safe_path(db_path):
            self._json_response(403, {"error": "Path not allowed"})
            return
        execute_streaming(self, f"sqlite3 {_shell_quote(db_path)} {_shell_quote(query)} 2>/dev/null || echo 'Install: pkg install sqlite'")

    def _handle_web_server(self, data: dict) -> None:
        action = data.get("action", "start").strip()
        port = str(data.get("port", 8080))
        directory = data.get("directory", get_current_dir()).strip()
        if action == "stop":
            execute_streaming(self, f"pkill -f 'python3 -m http.server {port}' 2>/dev/null && echo 'Server stopped' || echo 'No server running'")
        elif action == "status":
            execute_streaming(self, f"pgrep -f 'python3 -m http.server' >/dev/null 2>&1 && echo 'Server running' || echo 'Server not running'")
        else:
            cmd = f"cd {_shell_quote(directory)} && python3 -m http.server {port} 2>&1"
            execute_streaming(self, cmd)

    def _handle_git_op(self, data: dict) -> None:
        action = data.get("action", "clone").strip()
        repo_url = data.get("url", "").strip()
        directory = data.get("directory", "").strip()
        repo_dir = data.get("repo_dir", get_current_dir()).strip()

        if action == "clone":
            if not repo_url:
                self._json_response(400, {"error": "Missing 'url' for clone"})
                return
            if directory:
                execute_streaming(self, f"git clone {_shell_quote(repo_url)} {_shell_quote(directory)} 2>&1 | tail -10 || echo 'Clone failed'")
            else:
                execute_streaming(self, f"git clone {_shell_quote(repo_url)} 2>&1 | tail -10 || echo 'Clone failed'")
        elif action in ("status", "log", "diff", "pull", "push", "branch"):
            if not self._is_safe_path(repo_dir):
                self._json_response(403, {"error": "Path not allowed"})
                return
            if action == "log":
                n = data.get("limit", 5)
                execute_streaming(self, f"cd {_shell_quote(repo_dir)} && git log --oneline -{n} 2>&1 || echo 'Git failed'")
            elif action == "branch":
                execute_streaming(self, f"cd {_shell_quote(repo_dir)} && git branch -a 2>&1 || echo 'Git failed'")
            else:
                execute_streaming(self, f"cd {_shell_quote(repo_dir)} && git {action} 2>&1 | tail -20 || echo 'Git failed'")
        else:
            self._json_response(400, {"error": f"Unknown action: {action}"})

    def _is_safe_path(self, path: str) -> bool:
        """Prevent operations on sensitive system files."""
        blocked = ("/dev/", "/proc/", "/sys/")
        for prefix in blocked:
            if path.startswith(prefix):
                return False
        return True
