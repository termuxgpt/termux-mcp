import json
import os
import shutil
import threading
import time
import uuid

from ..security import get_risk_assessment
from . import base

TICK = 60
_thread = None
_stop = threading.Event()
_lock = threading.Lock()


def _rules():
    return base.load_state("watch", {"rules": []})


def _save(state):
    base.save_state("watch", state)


def _battery():
    res = base.sh("termux-battery-status", timeout=15, check_risk=False)
    try:
        return json.loads(res.out)
    except ValueError:
        return {}


def _wifi():
    res = base.sh("termux-wifi-connectioninfo", timeout=15, check_risk=False)
    try:
        return json.loads(res.out)
    except ValueError:
        return {}


def evaluate(cond: dict, rule: dict, cache: dict) -> bool:
    now = time.localtime()
    for key, want in cond.items():
        if key in ("battery_below", "charging"):
            b = cache.setdefault("battery", _battery())
            if not b:
                return False
            if key == "battery_below" and not (b.get("percentage", 101) < float(want)):
                return False
            if key == "charging":
                charging = str(b.get("status", "")).upper() in ("CHARGING", "FULL")
                if charging != base.boolish(want):
                    return False
        elif key == "wifi":
            w = cache.setdefault("wifi", _wifi())
            connected = bool(w) and w.get("supplicant_state") == "COMPLETED"
            if isinstance(want, bool) or str(want).lower() in ("true", "false"):
                if connected != base.boolish(want):
                    return False
            elif not connected or str(w.get("ssid", "")).strip('"') != str(want):
                return False
        elif key == "at":
            hh, mm = (int(x) for x in str(want).split(":", 1))
            today = time.strftime("%Y-%m-%d")
            if rule.get("_last_at") == today or (now.tm_hour, now.tm_min) < (hh, mm):
                return False
        elif key == "every_min":
            if time.time() - rule.get("_last_fire", 0) < float(want) * 60:
                return False
        elif key == "file_changed":
            path = os.path.expanduser(str(want))
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                return False
            prev = rule.get("_mtime")
            rule["_mtime"] = mtime
            if prev is None or mtime == prev:
                return False
        elif key == "port_down":
            from .intent_tools import port_listening
            if port_listening(int(want)):
                return False
        elif key == "storage_below_gb":
            if shutil.disk_usage(base.home()).free / 1e9 >= float(want):
                return False
        elif key == "log_match":
            if not _log_match(want, rule):
                return False
        else:
            return False
    return True


LOG_READ_MAX = 1_000_000


def _log_match(want: dict, rule: dict) -> bool:
    import re
    path = os.path.expanduser(str((want or {}).get("path", "")))
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    offset = rule.get("_offset")
    if offset is None or size < offset:
        rule["_offset"] = size if offset is None else 0
        if offset is None:
            return False
        offset = 0
    if size == offset:
        return False
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(LOG_READ_MAX)
    except OSError:
        return False
    rule["_offset"] = offset + len(chunk)
    try:
        rx = re.compile(str(want.get("pattern", "")), re.IGNORECASE)
    except re.error:
        return False
    hit = None
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        if rx.search(line):
            hit = line.strip()
    if hit is None:
        return False
    rule["_last_match"] = hit[:200]
    return True


def _fire(rule: dict) -> str:
    action = rule.get("action", {})
    out = []
    if action.get("cmd"):
        res = base.sh(action["cmd"], timeout=600, confirmed=bool(rule.get("confirmed")))
        out.append(f"cmd exit {res.exit}")
    if action.get("notify"):
        import shlex
        text = str(action["notify"]).replace("{match}", str(rule.get("_last_match", "")))
        base.sh("termux-notification --title TermuxGPT --content " +
                shlex.quote(text[:200]), timeout=10, check_risk=False)
        out.append("notified")
    return ", ".join(out)


def tick() -> list:
    fired = []
    with _lock:
        state = _rules()
        cache = {}
        for rule in state["rules"]:
            if not rule.get("enabled", True):
                continue
            try:
                active = evaluate(rule.get("when", {}), rule, cache)
            except Exception:
                active = False
            edge = active and not rule.get("_active")
            if any(k in rule.get("when", {}) for k in ("every_min", "at", "log_match")):
                edge = active
            rule["_active"] = active
            if edge:
                result = _fire(rule)
                rule["_last_fire"] = time.time()
                rule["fires"] = rule.get("fires", 0) + 1
                rule["last"] = f"{base.now_iso()} {result}"
                if "at" in rule.get("when", {}):
                    rule["_last_at"] = time.strftime("%Y-%m-%d")
                fired.append(rule["id"])
        _save(state)
    return fired


def _loop():
    while not _stop.wait(TICK):
        try:
            tick()
        except Exception:
            pass


def ensure_running() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not _rules()["rules"]:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="termux-mcp-reactor")
    _thread.start()


def add(when: dict, action: dict, name: str = "", confirmed: bool = False) -> dict:
    if not isinstance(when, dict) or not when:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "when: {condition: value, ...}"}]}
    if not isinstance(action, dict) or not (action.get("cmd") or action.get("notify")):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "action: {cmd} and/or {notify}"}]}
    known = {"battery_below", "charging", "wifi", "at", "every_min", "file_changed", "port_down", "storage_below_gb",
             "log_match"}
    bad = sorted(set(when) - known)
    if bad:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"unknown condition(s): {bad}"}],
                "known": sorted(known)}
    if "log_match" in when:
        lm = when["log_match"]
        if not isinstance(lm, dict) or not lm.get("path") or not lm.get("pattern"):
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "log_match: {path, pattern}"}]}
        import re
        try:
            re.compile(str(lm["pattern"]))
        except re.error as error:
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"log_match pattern: {error}"}]}
    if action.get("cmd"):
        from .. import kernel
        decision = kernel.gate(action["cmd"], confirmed=confirmed, tool="watch_add", spend_approval=False)
        if decision["status"] in ("blocked", "denied"):
            return {"ok": False, "errors": [{"code": decision["code"], "msg": decision["message"]}]}
        if decision["status"] == "confirm":
            return {"ok": False, "needs_confirmation": True,
                    "errors": [{"code": "E_CONFIRM", "msg": "the action is risky; resend with confirmed: true"}]}
    rule = {"id": "w-" + uuid.uuid4().hex[:6], "name": name[:60], "when": when, "action": action,
            "enabled": True, "confirmed": bool(confirmed), "created": base.now_iso(), "fires": 0}
    with _lock:
        state = _rules()
        state["rules"].append(rule)
        _save(state)
    ensure_running()
    return {"ok": True, "id": rule["id"], "summary": f"watching: {describe(rule)}"}


def describe(rule: dict) -> str:
    w = ", ".join(f"{k}={v}" for k, v in rule.get("when", {}).items())
    a = rule.get("action", {})
    return f"when {w} → " + (a.get("cmd") or f"notify '{a.get('notify')}'")


def list_rules() -> dict:
    rules = _rules()["rules"]
    return {"ok": True, "count": len(rules),
            "rules": [{"id": r["id"], "name": r.get("name", ""), "rule": describe(r),
                       "enabled": r.get("enabled", True), "fires": r.get("fires", 0), "last": r.get("last", "")}
                      for r in rules],
            "running": bool(_thread and _thread.is_alive()),
            "summary": f"{len(rules)} watch rule(s)"}


def remove(rule_id: str) -> dict:
    with _lock:
        state = _rules()
        before = len(state["rules"])
        state["rules"] = [r for r in state["rules"] if r["id"] != rule_id]
        _save(state)
    return {"ok": True, "removed": before - len(state["rules"]),
            "summary": f"removed {before - len(state['rules'])} rule(s)"}


def set_enabled(rule_id: str, enabled: bool) -> dict:
    with _lock:
        state = _rules()
        for r in state["rules"]:
            if r["id"] == rule_id:
                r["enabled"] = bool(enabled)
        _save(state)
    return {"ok": True, "summary": f"{rule_id} {'enabled' if enabled else 'paused'}"}
