import json
import logging
import re
from typing import List, Optional


from .config import MAX_OUTPUT_BYTES
from .handler import MCPHandler
from .handlers.ai_power import handle_smart_install, handle_optimize
from .handlers.features import (
    handle_system_info, handle_process_list, handle_process_kill,
    handle_cron_add, handle_cron_list, handle_cron_remove,
    handle_diff, handle_health, handle_cloud_sync, handle_git_pr,
    handle_recipe_list, handle_recipe_run, handle_recipe_save,
    handle_context, handle_context_save,
)
from .handlers.history import (
    handle_history_list, handle_history_save, handle_history_clear,
)
from .handlers.terminal import (
    handle_diagnose, handle_backup, handle_restore,
)
from .tools_schema import OPENAI_TOOLS


logger = logging.getLogger(__name__)


class CapturingWriter:

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode()
        self.buffer.extend(data)
        return len(data)

    def flush(self) -> None:
        pass


def dechunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:

        j = data.find(b"\r\n", i)
        if j == -1:
            break
        size = int(data[i:j], 16)
        if size == 0:
            break
        out += data[j + 2: j + 2 + size]
        i = j + 2 + size
        if i + 1 < n and data[i:i + 2] == b"\r\n":
            i += 2
        elif i < n and data[i:i + 1] == b"\n":
            i += 1
    return bytes(out)


class VirtualHandler:

    def __init__(self) -> None:
        self.status: int = 200
        self._headers = []
        self._chunked = False
        self.wfile = CapturingWriter()

    def send_response(self, status: int, message=None) -> None:
        self.status = int(status)

    def send_header(self, keyword: str, value) -> None:
        self._headers.append((str(keyword).lower(), str(value)))

    def end_headers(self) -> None:
        self._chunked = any(k == "transfer-encoding" and v.lower() == "chunked"
                            for k, v in self._headers)

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("[virtual] " + fmt, *args)

    def header(self, name: str) -> Optional[str]:
        for k, v in self._headers:
            if k == name.lower():
                return v
        return None

    @property
    def content_type(self) -> str:
        return self.header("content-type") or "text/plain"

    def body(self) -> bytes:
        raw = bytes(self.wfile.buffer)
        return dechunk(raw) if self._chunked else raw

    def decoded_text(self) -> str:
        return self.body().decode("utf-8", errors="ignore")

    def parsed_json(self) -> Optional[dict]:
        try:
            return json.loads(self.decoded_text())
        except (ValueError, TypeError):
            return None


TRUNCATION_MARKER = (
    f"\n[Output truncated: max {MAX_OUTPUT_BYTES} bytes — "
    f"full output not sent]\n"
)


def analyze_output(text: str) -> dict:
    meta = {"exit_code": None, "cancelled": False,
            "timed_out": False, "error": False, "truncated": False}
    clean = text

    if clean.endswith("\nCancelled\n"):
        meta["cancelled"] = True
        clean = clean[:-len("\nCancelled\n")]

    if clean.endswith("\nExit: (process detached)\n"):
        clean = clean[:-len("\nExit: (process detached)\n")]
    m = re.search(r"\nExit: (\d+)\n$", clean)
    if m:
        meta["exit_code"] = int(m.group(1))
        clean = clean[:m.start()]
    if clean.endswith("\nDone\n"):
        clean = clean[:-len("\nDone\n")]
    if clean.endswith("\n✅ Done\n"):
        clean = clean[:-len("\n✅ Done\n")]
    m = re.search(r"\n❌ Exit code: (\d+)\n$", clean)
    if m:
        meta["exit_code"] = int(m.group(1))
        clean = clean[:m.start()]
    if re.search(r"\n⏱️ Timed out after \d+s\n$", clean):
        meta["timed_out"] = True
        clean = re.sub(r"\n⏱️ Timed out after \d+s\n$", "", clean)
    for marker in ("\n❌ Error: ", "\nError: "):
        idx = clean.rfind(marker)
        if idx != -1:
            meta["error"] = True
            clean = clean[:idx]
            break

    if clean.endswith("\n"):
        clean = clean[:-1]
    clean = clean.rstrip("\n")
    if TRUNCATION_MARKER.strip() in text:
        meta["truncated"] = True
    meta["text"] = clean
    return meta


def decode_virtual(vh: VirtualHandler) -> dict:
    ct = vh.content_type
    if "json" in ct:
        data = vh.parsed_json() or {}
        raw = vh.decoded_text().strip()
        error = (vh.status >= 400 or bool(data.get("error"))
                 or data.get("blocked") is True)
        text = raw
        if error and not text:
            text = data.get("error") or f"HTTP {vh.status}"
        if data.get("requires_confirmation") and not error:
            text = (raw + "\n\nThis action needs confirmation — re-invoke "
                    "this tool with `confirmed: true` to proceed.")
        return {"text": text or "(no output)", "is_error": error}
    meta = analyze_output(vh.decoded_text())
    code = meta["exit_code"]
    is_error = (meta["error"] or meta["timed_out"]
                or (code is not None and code != 0))
    return {"text": meta["text"] or "(no output)", "is_error": is_error}


WS_ALIAS_DROPS = {"camera", "wifi", "sms", "tts", "ocr"}


NATIVE_TOOL_NAMES = {"run", "cancel", "session_start", "session_run",
                     "session_poll", "session_list", "session_kill"}


_INSTANCE_ROUTES = {
    "ls": "_handle_ls", "read": "_handle_read", "write": "_handle_write",
    "mkdir": "_handle_mkdir", "delete": "_handle_delete",
    "search": "_handle_search", "battery": "_handle_battery",
    "location": "_handle_location", "wifi_info": "_handle_wifi_info",
    "screenshot": "_handle_screenshot", "camera_photo": "_handle_camera_photo",
    "notify": "_handle_notify", "sms_send": "_handle_sms_send",
    "sms_inbox": "_handle_sms_inbox", "tts_speak": "_handle_tts_speak",
    "open_url": "_handle_open_url", "download": "_handle_download",
    "public_ip": "_handle_public_ip", "weather": "_handle_weather",
    "speedtest": "_handle_speedtest", "qrcode": "_handle_qrcode",
    "image_process": "_handle_image_process",
    "clipboard_get": "_handle_clipboard_get",
    "clipboard_set": "_handle_clipboard_set",
    "toast": "_handle_toast", "share": "_handle_share",
    "text_extract": "_handle_text_extract",
}


_MODULE_ROUTES = {
    "system_info": handle_system_info,
    "health": handle_health,
    "process_list": handle_process_list,
    "process_kill": handle_process_kill,
    "cron_add": handle_cron_add,
    "cron_list": handle_cron_list,
    "cron_remove": handle_cron_remove,
    "cloud_sync": handle_cloud_sync,
    "diff": handle_diff,
    "smart_install": handle_smart_install,
    "diagnose": handle_diagnose,
    "optimize": handle_optimize,
    "git_pr": handle_git_pr,
    "backup": handle_backup,
    "restore": handle_restore,
    "recipe_list": handle_recipe_list,
    "recipe_run": handle_recipe_run,
    "recipe_save": handle_recipe_save,
    "context": handle_context,
    "context_save": handle_context_save,
    "history": handle_history_list,
    "history_save": handle_history_save,
    "history_clear": handle_history_clear,
}


_ghost = None


def _bound_method(name: str):
    global _ghost
    if _ghost is None:
        _ghost = MCPHandler.__new__(MCPHandler)
    return getattr(_ghost, name)


def route_callable(tool_name: str):
    if tool_name in _INSTANCE_ROUTES:
        return _bound_method(_INSTANCE_ROUTES[tool_name])
    if tool_name in _MODULE_ROUTES:
        return _MODULE_ROUTES[tool_name]
    return None


def bridge_tool_names() -> List[str]:
    return [n for n, _ in _iter_catalog_defs() if n not in NATIVE_TOOL_NAMES]


def _iter_catalog_defs():
    for entry in OPENAI_TOOLS:
        fn = entry.get("function", entry)
        name = fn.get("name", "")
        if not name or name in WS_ALIAS_DROPS:
            continue
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        yield name, {
            "name": name,
            "description": fn.get("description", ""),
            "inputSchema": params,
        }


_EXTRA_TOOL_DEFS = [
    {
        "name": "text_extract",
        "description": "Extract text from an image via OCR (Tesseract).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string",
                          "description": "Image file path"},
                "lang": {"type": "string",
                         "description": "OCR language", "default": "eng"},
            },
            "required": ["input"],
        },
    },
]


EXTRA_NAMES = [d["name"] for d in _EXTRA_TOOL_DEFS]


def build_mcp_tool_list(native_defs=None,
                        filters: tuple = ((), ())) -> List[dict]:
    by_name = {}
    for name, d in _iter_catalog_defs():
        by_name[name] = d
    for d in _EXTRA_TOOL_DEFS:
        by_name[d["name"]] = d
    for d in (native_defs or []):
        by_name[d["name"]] = d

    includes, excludes = filters
    ordered = []

    seen = set()
    for name, d in _iter_catalog_defs():
        if name not in by_name:
            continue
        seen.add(name)
        ordered.append(by_name[name])
    for d in list(_EXTRA_TOOL_DEFS) + list(native_defs or []):
        if d["name"] not in seen:
            seen.add(d["name"])
            ordered.append(d)

    def keep(d: dict) -> bool:
        n = d["name"]
        if n in excludes:
            return False
        return not includes or n in includes

    return [d for d in ordered if keep(d)]


def tool_name_allowed(name: str, filters: tuple = ((), ())) -> bool:
    includes, excludes = filters
    if name in excludes:
        return False
    return not includes or name in includes
