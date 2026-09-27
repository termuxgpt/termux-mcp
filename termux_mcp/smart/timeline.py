from .. import changes, safety
from . import base


def _entries():
    return changes.read(safety.safety_root(""), limit=2000)


def _titles() -> dict:
    tx = base.load_state("tx", {})
    return {k: v.get("title", "") for k, v in tx.items()}


def timeline(limit: int = 20, include_untracked: bool = True) -> dict:
    groups, order = {}, []
    for e in _entries():
        task = str(e.get("task") or "")
        key = task or ("untracked:" + str(e.get("ts", ""))[:13])
        if not task and not include_untracked:
            continue
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"task": task or None, "first": e.get("ts", ""), "last": e.get("ts", ""),
                               "create": 0, "modify": 0, "delete": 0, "tools": set(), "files": [],
                               "revertable": 0}
            order.append(key)
        g["first"] = e.get("ts", g["first"])
        kind = e.get("kind")
        if kind in ("create", "modify", "delete"):
            g[kind] += 1
        if e.get("tool"):
            g["tools"].add(e["tool"])
        if e.get("path") and e["path"] not in g["files"] and len(g["files"]) < 8:
            g["files"].append(e["path"])
        if changes.revertable(e):
            g["revertable"] += 1
    titles = _titles()
    rows = []
    for key in order[:max(1, min(int(limit or 20), 100))]:
        g = groups[key]
        g["tools"] = sorted(g["tools"])
        g["title"] = titles.get(g["task"] or "", "") or (", ".join(g["tools"]) if g["tools"] else "")
        g["status"] = base.load_state("tx", {}).get(g["task"] or "", {}).get("status", "")
        rows.append(g)
    return {"ok": True, "tasks": rows,
            "summary": f"{len(rows)} task(s) with changes; undo any with undo_task"}


def undo_task(task: str, confirmed: bool = False) -> dict:
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "task: an id from timeline"}]}
    all_entries = _entries()
    mine = [e for e in all_entries if str(e.get("task") or "") == task]
    if not mine:
        return {"ok": False, "errors": [{"code": "E_NO_TASK", "msg": f"no changes recorded for {task}"}]}
    oldest_ts = min(str(e.get("ts", "")) for e in mine)
    paths = {e.get("path") for e in mine}
    later = [e for e in all_entries if str(e.get("ts", "")) > oldest_ts and e.get("path") in paths
             and str(e.get("task") or "") != task]
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "changes": len(mine),
                "files": sorted(p for p in paths if p)[:30],
                "conflicts": sorted({e["path"] for e in later})[:20],
                "errors": [{"code": "E_CONFIRM", "msg": ("later tasks also changed some of these files; "
                                                         if later else "") + "resend with confirmed: true"}]}
    done = changes.revert(mine, snapshot_before=lambda p: safety.snapshot_before_write(p, tool="undo_task"))
    state = base.load_state("tx", {})
    if task in state:
        state[task]["status"] = "rolled_back"
        base.save_state("tx", state)
    restored = sum(1 for _, r in done if r in ("restored", "removed"))
    return {"ok": True, "task": task, "reverted": restored, "conflicts": len(later),
            "results": [{"path": p, "result": r} for p, r in done[:40]],
            "summary": f"undid {restored} of {len(done)} change(s) from {task}"}
