import threading

from . import base

_lock = threading.Lock()
_STATE = "meter"


def _load():
    return base.load_state(_STATE, {"tools": {}, "tasks": {}, "since": base.now_iso()})


def record(tool: str, raw_bytes: int, sent_bytes: int, task_id: str = "",
           ai_calls_avoided: int = 0) -> None:
    with _lock:
        state = _load()
        row = state["tools"].setdefault(tool, {"calls": 0, "raw": 0, "sent": 0,
                                               "avoided": 0})
        row["calls"] += 1
        row["raw"] += max(int(raw_bytes), 0)
        row["sent"] += max(int(sent_bytes), 0)
        row["avoided"] += int(ai_calls_avoided)
        if task_id:
            t = state["tasks"].setdefault(task_id[:64], {"calls": 0, "raw": 0, "sent": 0})
            t["calls"] += 1
            t["raw"] += max(int(raw_bytes), 0)
            t["sent"] += max(int(sent_bytes), 0)
            if len(state["tasks"]) > 300:
                for key in list(state["tasks"])[:50]:
                    state["tasks"].pop(key, None)
        base.save_state(_STATE, state)


def report(task_id: str = "") -> dict:
    state = _load()
    if task_id:
        t = state["tasks"].get(task_id[:64])
        if not t:
            return {"ok": True, "task": task_id, "calls": 0, "saved_tokens": 0}
        saved = max(t["raw"] - t["sent"], 0) // 4
        return {"ok": True, "task": task_id, "calls": t["calls"],
                "raw_tokens": t["raw"] // 4, "sent_tokens": t["sent"] // 4,
                "saved_tokens": saved,
                "summary": f"saved ~{saved:,} tokens over {t['calls']} calls"}
    raw = sum(r["raw"] for r in state["tools"].values())
    sent = sum(r["sent"] for r in state["tools"].values())
    avoided = sum(r.get("avoided", 0) for r in state["tools"].values())
    calls = sum(r["calls"] for r in state["tools"].values())
    top = sorted(state["tools"].items(), key=lambda kv: -(kv[1]["raw"] - kv[1]["sent"]))[:8]
    saved = max(raw - sent, 0) // 4
    return {"ok": True, "since": state.get("since"), "calls": calls,
            "raw_tokens": raw // 4, "sent_tokens": sent // 4,
            "saved_tokens": saved, "ai_calls_avoided": avoided,
            "top": [{"tool": k, "calls": v["calls"],
                     "saved_tokens": max(v["raw"] - v["sent"], 0) // 4} for k, v in top],
            "summary": f"saved ~{saved:,} tokens and {avoided} AI calls since {state.get('since')}"}


def reset() -> dict:
    base.save_state(_STATE, {"tools": {}, "tasks": {}, "since": base.now_iso()})
    return {"ok": True, "summary": "meter reset"}
