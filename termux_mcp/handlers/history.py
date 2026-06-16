import json
import os
import time
from typing import TYPE_CHECKING

from ..utils import json_response

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

HISTORY_FILE = os.path.join(
    os.environ.get("HOME", "/data/data/com.termux/files/home"),
    ".termux_history.json",
)
MAX_ENTRIES = 2000


def _load() -> list:
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list) -> None:
    # Trim to max entries
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(entries, f)


def handle_history_list(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    entries = _load()
    json_response(handler, 200, {"entries": entries, "count": len(entries)})


def handle_history_save(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    raw_input = (data.get("rawInput") or data.get("raw_input") or "").strip()
    output = (data.get("output") or "").strip()
    if not raw_input and not output:
        json_response(handler, 400, {"error": "Missing rawInput or output"})
        return

    entries = _load()
    ran_cmd = (data.get("ranCommand") or "").strip()
    entry = {
        "rawInput": raw_input,
        "output": output[:5000] if len(output) > 5000 else output,
        "ranCommand": ran_cmd if ran_cmd else None,
        "success": data.get("success", True),
        "traces": data.get("traces") or data.get("agentTraces") or [],
        "timestamp": time.time(),
    }
    entries.append(entry)
    _save(entries)
    json_response(handler, 200, {"saved": True, "total": len(entries)})


def handle_history_clear(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    try:
        os.remove(HISTORY_FILE)
        json_response(handler, 200, {"cleared": True})
    except FileNotFoundError:
        json_response(handler, 200, {"cleared": True})
    except Exception as e:
        json_response(handler, 500, {"error": str(e)})
