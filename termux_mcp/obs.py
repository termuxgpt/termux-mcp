import json
import os
import threading
import time
from contextlib import contextmanager

LOG_MAX_BYTES = 1_000_000
SAMPLES_KEPT = 200

_lock = threading.Lock()
_started = time.time()
_tools = {}
_codes = {}


def _log_path() -> str:
    from .auth import config_dir
    folder = os.path.join(config_dir(), "logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "events.jsonl")


def _enabled() -> bool:
    return os.environ.get("TERMUX_MCP_LOG_EVENTS", "1").strip().lower() not in ("0", "off", "false", "no")


def _write(event: dict) -> None:
    if not _enabled():
        return
    try:
        path = _log_path()
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass


def record(transport: str, tool: str, ms: float, ok: bool, code: str = "") -> None:
    tool = str(tool or "?")[:60]
    with _lock:
        row = _tools.setdefault(tool, {"count": 0, "errors": 0, "samples": [], "codes": {}, "transports": {}})
        row["count"] += 1
        row["transports"][transport] = row["transports"].get(transport, 0) + 1
        if not ok:
            row["errors"] += 1
        if code:
            row["codes"][code] = row["codes"].get(code, 0) + 1
            _codes[code] = _codes.get(code, 0) + 1
        row["samples"].append(round(ms, 1))
        if len(row["samples"]) > SAMPLES_KEPT:
            del row["samples"][:len(row["samples"]) - SAMPLES_KEPT]
    _write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "transport": transport, "tool": tool,
            "ms": round(ms, 1), "ok": bool(ok), **({"code": code} if code else {})})


def code_of(result) -> str:
    if not isinstance(result, dict):
        return ""
    body = result.get("digest") if isinstance(result.get("digest"), dict) else result
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("code", ""))[:40]
    return ""


class _Call:
    __slots__ = ("ok", "code")

    def __init__(self):
        self.ok = True
        self.code = ""

    def result(self, result) -> None:
        if isinstance(result, dict):
            self.ok = not result.get("is_error") and result.get("ok", True) is not False
            self.code = code_of(result)


@contextmanager
def timed(transport: str, tool: str):
    call = _Call()
    start = time.monotonic()
    try:
        yield call
    except Exception:
        call.ok = False
        call.code = call.code or "E_INTERNAL"
        raise
    finally:
        record(transport, tool, (time.monotonic() - start) * 1000, call.ok, call.code)


def _pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def metrics() -> dict:
    with _lock:
        tools = {name: {"count": r["count"], "errors": r["errors"],
                        "error_rate": round(r["errors"] / r["count"], 3) if r["count"] else 0.0,
                        "p50_ms": _pct(r["samples"], 0.5), "p95_ms": _pct(r["samples"], 0.95),
                        "codes": dict(r["codes"]), "transports": dict(r["transports"])}
                 for name, r in _tools.items()}
        codes = dict(_codes)
    fixes = {}
    try:
        from .smart import fixgraph
        for fid, row in fixgraph._stats().get("fixes", {}).items():
            fixes[fid] = {"tries": row.get("tries", 0), "ok": row.get("ok", 0),
                          "rate": round(row.get("ok", 0) / max(row.get("tries", 1), 1), 2)}
    except Exception:
        pass
    total = sum(t["count"] for t in tools.values())
    errors = sum(t["errors"] for t in tools.values())
    return {"ok": True, "uptime_s": int(time.time() - _started), "calls": total, "errors": errors,
            "tools": tools, "error_codes": codes, "fixes": fixes,
            "summary": f"{total} call(s), {errors} error(s) across {len(tools)} tool(s)"}


def reset() -> None:
    with _lock:
        _tools.clear()
        _codes.clear()
