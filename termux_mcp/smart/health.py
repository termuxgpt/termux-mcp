import json
import os
import shutil
import time

from . import base, reactor


def _check_all() -> dict:
    home = base.home()
    items = []

    def add(name, ok, detail, fix=""):
        items.append({"check": name, "ok": ok, "detail": detail, **({"fix": fix} if fix and not ok else {})})

    du = shutil.disk_usage(home)
    free_gb = du.free / 1e9
    add("storage", free_gb > 1.0, f"{free_gb:.1f} GB free", "storage_clean")
    b = base.sh("termux-battery-status", timeout=15, check_risk=False)
    try:
        bj = json.loads(b.out)
        add("battery", bj.get("percentage", 100) > 15, f"{bj.get('percentage')}% {bj.get('status', '').lower()}"
            f", {bj.get('temperature', 0):.0f}°C")
        add("termux_api", True, "responding")
    except ValueError:
        add("termux_api", False, "no answer from Termux:API", "pkg install termux-api + Termux:API app")
    lists = os.path.join(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "var", "lib", "apt", "lists")
    try:
        age_days = (time.time() - os.path.getmtime(lists)) / 86400
        add("package_index", age_days < 14, f"updated {age_days:.0f} days ago", "pkg update -y")
    except OSError:
        add("package_index", False, "never updated", "pkg update -y")
    audit = base.sh("dpkg --audit 2>/dev/null | head -5", timeout=30, check_risk=False)
    add("packages", not audit.out.strip(), "consistent" if not audit.out.strip() else audit.out.strip()[:120],
        "dpkg --configure -a")
    add("shared_storage", os.path.isdir(os.path.join(home, "storage", "shared")),
        "linked" if os.path.isdir(os.path.join(home, "storage", "shared")) else "not linked", "termux-setup-storage")
    from . import intent_tools
    dead = [n for n, _, port, pid, _ in intent_tools.service_table() if not pid]
    add("services", not dead, "all up" if not dead else f"down: {', '.join(dead)}", "service_ensure")
    mem = base.sh("free -m 2>/dev/null | awk '/Mem:/{print $7}'", timeout=5, check_risk=False).out.strip()
    if mem.isdigit():
        add("memory", int(mem) > 300, f"{mem} MB available")
    bad = [i for i in items if not i["ok"]]
    return {"ok": True, "at": base.now_iso(), "healthy": not bad, "problems": len(bad), "checks": items,
            "summary": "all good" if not bad else "; ".join(f"{i['check']}: {i['detail']}" for i in bad)}


def run() -> dict:
    report = _check_all()
    state = base.load_state("health", {"reports": []})
    state["reports"] = (state.get("reports", []) + [report])[-14:]
    base.save_state("health", state)
    return report


def report() -> dict:
    state = base.load_state("health", {"reports": []})
    if not state.get("reports"):
        return run()
    last = dict(state["reports"][-1])
    last["history"] = [{"at": r["at"], "problems": r["problems"]} for r in state["reports"][-7:]]
    last["cached"] = True
    return last


def enable(at: str = "09:00") -> dict:
    existing = [r for r in reactor.list_rules()["rules"] if r.get("name") == "health_watch"]
    if existing:
        return {"ok": True, "summary": "daily health check already enabled", "id": existing[0]["id"]}
    return reactor.add({"at": at}, {"cmd": "python -m termux_mcp.smart.health_cli"}, name="health_watch")


def disable() -> dict:
    removed = 0
    for r in reactor.list_rules()["rules"]:
        if r.get("name") == "health_watch":
            removed += reactor.remove(r["id"])["removed"]
    return {"ok": True, "removed": removed, "summary": "daily health check disabled"}
