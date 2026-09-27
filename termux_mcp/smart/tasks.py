import threading
import time
import uuid

_lock = threading.Lock()
_tasks = {}
KEEP = 50


def start(tool: str, params: dict, runner) -> dict:
    tid = "t-" + uuid.uuid4().hex[:8]
    record = {"id": tid, "tool": tool, "status": "running", "started": time.time(), "result": None}
    with _lock:
        _tasks[tid] = record
        if len(_tasks) > KEEP:
            for old in sorted(_tasks, key=lambda k: _tasks[k]["started"])[:len(_tasks) - KEEP]:
                if _tasks[old]["status"] != "running":
                    _tasks.pop(old, None)

    def work():
        try:
            out = runner(tool, params or {})
            record["result"] = out.get("digest") or out
            record["status"] = "done" if not out.get("is_error") else "failed"
        except Exception as error:
            record["result"] = {"ok": False, "errors": [{"code": "E_TASK", "msg": str(error)}]}
            record["status"] = "failed"
        record["finished"] = time.time()

    threading.Thread(target=work, daemon=True, name=f"task-{tid}").start()
    return {"ok": True, "task": tid, "status": "running", "summary": f"started {tool} as {tid}; poll task_status"}


def status(tid: str) -> dict:
    rec = _tasks.get(tid)
    if not rec:
        return {"ok": False, "errors": [{"code": "E_NO_TASK", "msg": f"no task {tid}"}]}
    body = {"ok": True, "task": tid, "tool": rec["tool"], "status": rec["status"],
            "secs": round((rec.get("finished") or time.time()) - rec["started"], 1)}
    if rec["status"] != "running":
        body["result"] = rec["result"]
    return body


def list_tasks() -> dict:
    with _lock:
        rows = [{"task": t["id"], "tool": t["tool"], "status": t["status"]} for t in _tasks.values()]
    return {"ok": True, "tasks": rows[-20:], "summary": f"{sum(r['status'] == 'running' for r in rows)} running"}
