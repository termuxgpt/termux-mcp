import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

from . import base, digest

_lock = threading.Lock()


def _drafts_dir() -> str:
    return base.ensure_dir(base.root("learn", "drafts"))


def _step_cmd(step):
    if isinstance(step, str):
        return step.strip(), None
    if not isinstance(step, dict):
        return "", None
    cmd = step.get("cmd") or step.get("run") or ""
    if not cmd and step.get("tool") in ("run", "run_digest"):
        cmd = (step.get("params") or {}).get("cmd", "")
    ok = step.get("ok")
    if ok is None and "exit" in step:
        ok = step.get("exit") == 0
    return str(cmd or "").strip(), ok


def compact(steps) -> dict:
    from .. import kernel
    rows = []
    for step in steps or []:
        cmd, ok = _step_cmd(step)
        if not cmd or ok is False:
            continue
        cmd = re.sub(r"^echo '[^']*snapshot: [^']*'; ", "", cmd)
        rows.append((cmd, kernel.looks_mutating(cmd)))
    seen, uniq = set(), []
    for cmd, mut in rows:
        if cmd in seen:
            continue
        seen.add(cmd)
        uniq.append((cmd, mut))
    last_change = max((i for i, (_, mut) in enumerate(uniq) if mut), default=-1)
    kept = [cmd for i, (cmd, mut) in enumerate(uniq) if mut]
    verify = ""
    if last_change >= 0:
        after = [cmd for cmd, mut in uniq[last_change + 1:] if not mut]
        verify = after[-1] if after else ""
    else:
        kept = [cmd for cmd, _ in uniq]
    dropped = len(rows) - len(kept) - (1 if verify else 0)
    return {"steps": kept, "verify": verify, "dropped": max(dropped, 0)}


def learn_propose(steps=None, task_id: str = "", title: str = "", request: str = "") -> dict:
    from .. import kernel, playbook
    if not steps and task_id:
        steps = kernel.actions_for(task_id)
    if not steps:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "steps or a task_id with recorded actions"}]}
    plan = compact(steps)
    if not plan["steps"]:
        return {"ok": False, "errors": [{"code": "E_NOTHING", "msg": "nothing worth saving: no successful steps"}]}
    title = (title or request or "Saved task").strip()[:60]
    phrases = [request.strip()] if request and request.strip() else None
    commands = plan["steps"] + ([plan["verify"]] if plan["verify"] else [])
    draft_id = "d-" + uuid.uuid4().hex[:8]
    folder = os.path.join(_drafts_dir(), draft_id)
    res = playbook.harvest(commands, title=title, phrases=phrases,
                           playbook_id=playbook._slug(title) or "saved_task",
                           overwrite=True, playbook_dir=folder)
    if not res.get("ok"):
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": False, "errors": [{"code": "E_HARVEST", "msg": "; ".join(map(str, res.get("errors", [])))}],
                "draft": res.get("draft")}
    draft = res["draft"]
    if plan["verify"]:
        verify_step = draft["steps"].pop()
        draft["steps"][-1]["verify"] = verify_step["run"]
    draft["learned"] = {"at": base.now_iso(), "from_task": task_id, "dropped": plan["dropped"]}
    with open(res["path"], "w", encoding="utf-8") as handle:
        json.dump(draft, handle, indent=2)
    return {"ok": True, "draft_id": draft_id, "playbook": draft["id"], "title": draft["title"],
            "steps": [s["run"] for s in draft["steps"]], "verify": draft["steps"][-1].get("verify", ""),
            "slots": res.get("slots", []), "dropped": plan["dropped"],
            "summary": f"{len(draft['steps'])} step(s) → '{draft['title']}'"
                       f"{' + verify' if plan['verify'] else ''}; learn_save to keep it"}


def learn_save(draft_id: str, title: str = "", phrases=None, overwrite: bool = False) -> dict:
    from .. import playbook
    draft_id = re.sub(r"[^\w-]", "", str(draft_id or ""))
    folder = os.path.join(_drafts_dir(), draft_id)
    files = [f for f in os.listdir(folder)] if os.path.isdir(folder) else []
    if not draft_id or not files:
        return {"ok": False, "errors": [{"code": "E_NO_DRAFT", "msg": f"no draft {draft_id}"}]}
    with open(os.path.join(folder, files[0]), encoding="utf-8") as handle:
        draft = json.load(handle)
    if title:
        draft["title"] = str(title)[:60]
        draft["id"] = playbook._slug(title) or draft["id"]
    if phrases:
        draft["phrases"] = [str(p) for p in phrases if str(p).strip()] or draft["phrases"]
    target_dir = playbook.user_dir()
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f"{draft['id']}.json")
    if os.path.exists(target) and not overwrite:
        return {"ok": False, "errors": [{"code": "E_EXISTS", "msg": f"{draft['id']} exists; pass overwrite"}]}
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(draft, handle, indent=2)
        handle.write("\n")
    shutil.rmtree(folder, ignore_errors=True)
    try:
        from . import semantic
        semantic.invalidate()
    except Exception:
        pass
    return {"ok": True, "playbook": draft["id"], "path": target, "phrases": draft["phrases"],
            "summary": f"saved '{draft['title']}' — say \"{draft['phrases'][0]}\" to replay it with no AI"}


PROMOTE_AFTER = 2
_VOLATILE = re.compile(r"(/[\w.@+-]+)+|\b\d+(\.\d+)*\b|0x[0-9a-f]+|'[^']*'|\"[^\"]*\"")


def fingerprint(error_text: str = "", code: str = "") -> str:
    if not code:
        found = digest.classify(error_text or "", 1)
        code = found[0]["code"] if found else "E_UNKNOWN"
    lines = [ln.strip() for ln in str(error_text or "").splitlines() if ln.strip()]
    key_line = ""
    for ln in reversed(lines):
        if re.search(r"error|fail|not found|denied|cannot|unable|no such", ln, re.IGNORECASE):
            key_line = ln
            break
    norm = _VOLATILE.sub("~", key_line.lower())[:120]
    return f"{code}:{hashlib.sha1(norm.encode()).hexdigest()[:10]}"


def _fixes():
    return base.load_state("fix_learned", {"candidates": {}, "promoted": {}})


def fix_learn(error: str = "", code: str = "", fix=None, verify: str = "", worked: bool = True,
              desc: str = "") -> dict:
    steps = [s for s in ([fix] if isinstance(fix, str) else (fix or [])) if str(s).strip()]
    if not steps:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "fix: the command(s) that fixed it"}]}
    if not (error or code):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "error text or code"}]}
    from ..security import get_risk_assessment
    if any(get_risk_assessment(s)["blocked"] for s in steps):
        return {"ok": False, "errors": [{"code": "E_BLOCKED", "msg": "a fix step is blocked by the safety filter"}]}
    fp = fingerprint(error, code)
    ecode = fp.split(":")[0]
    key = hashlib.sha1(("\n".join(steps) + "|" + verify).encode()).hexdigest()[:8]
    fid = f"learned_{ecode.lower()[2:]}_{key}"
    risk = "write"
    if all(not _mutates(s) for s in steps):
        risk = "safe"
    with _lock:
        st = _fixes()
        if fid in st["promoted"]:
            row = st["promoted"][fid]
            row["tries"] += 1
            row["ok"] += 1 if worked else 0
            base.save_state("fix_learned", st)
            return {"ok": True, "id": fid, "status": "promoted", "success_rate": round(row["ok"] / row["tries"], 2),
                    "summary": f"{fid} already known; stats updated"}
        cand = st["candidates"].setdefault(fid, {"id": fid, "code": ecode, "fingerprint": fp, "steps": steps,
                                                 "verify": verify, "risk": risk, "ok": 0, "tries": 0,
                                                 "desc": desc[:120] or f"Learned fix for {ecode}",
                                                 "sample": str(error)[:200], "created": base.now_iso()})
        cand["tries"] += 1
        cand["ok"] += 1 if worked else 0
        status = "candidate"
        if cand["ok"] >= PROMOTE_AFTER:
            st["promoted"][fid] = st["candidates"].pop(fid)
            status = "promoted"
        base.save_state("fix_learned", st)
    return {"ok": True, "id": fid, "status": status, "fingerprint": fp,
            "summary": f"{fid}: {status}" + (f" ({cand['ok']}/{PROMOTE_AFTER} successes)" if status == "candidate"
                                             else " — FixGraph now returns it")}


def _mutates(cmd: str) -> bool:
    from .. import kernel
    return kernel.looks_mutating(cmd)


def learned_for(code: str, error_text: str = "") -> list:
    rows = []
    fp = fingerprint(error_text, code) if error_text else ""
    for f in _fixes()["promoted"].values():
        if f["code"] != code:
            continue
        exact = bool(fp) and f["fingerprint"] == fp
        rate = round(f["ok"] / max(f["tries"], 1), 2)
        rows.append({"id": f["id"], "code": code, "risk": f["risk"], "desc": f["desc"], "steps": list(f["steps"]),
                     **({"verify": f["verify"]} if f.get("verify") else {}), "success_rate": rate,
                     "learned": True, "_exact": exact})
    rows.sort(key=lambda r: (not r["_exact"], -r["success_rate"]))
    for r in rows:
        r.pop("_exact", None)
    return rows


def learned_spec(fid: str):
    return _fixes()["promoted"].get(fid)


def learned_record(fid: str, worked: bool) -> None:
    with _lock:
        st = _fixes()
        row = st["promoted"].get(fid)
        if row:
            row["tries"] += 1
            row["ok"] += 1 if worked else 0
            base.save_state("fix_learned", st)


def fix_share(action: str, file: str = "", confirmed: bool = False) -> dict:
    if action == "list":
        st = _fixes()
        return {"ok": True, "promoted": [{"id": f["id"], "code": f["code"], "desc": f["desc"],
                                          "rate": round(f["ok"] / max(f["tries"], 1), 2)} for f in st["promoted"].values()],
                "candidates": [{"id": f["id"], "code": f["code"], "ok": f["ok"]} for f in st["candidates"].values()],
                "summary": f"{len(st['promoted'])} learned fix(es), {len(st['candidates'])} candidate(s)"}
    if action == "export":
        st = _fixes()
        path = file or os.path.join(base.ensure_dir(base.root("learn")), f"fixes-{time.strftime('%Y%m%d')}.json")
        path = os.path.expanduser(path)
        body = {"kind": "termux-mcp-fixes", "version": 1, "fixes": list(st["promoted"].values())}
        body["sha256"] = hashlib.sha256(json.dumps(body["fixes"], sort_keys=True).encode()).hexdigest()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=1)
        return {"ok": True, "file": path, "count": len(body["fixes"]), "summary": f"exported {len(body['fixes'])} fix(es)"}
    if action == "import":
        path = os.path.expanduser(file)
        try:
            with open(path, encoding="utf-8") as handle:
                body = json.load(handle)
        except (OSError, ValueError) as error:
            return {"ok": False, "errors": [{"code": "E_NO_FILE", "msg": str(error)}]}
        fixes = body.get("fixes") if isinstance(body, dict) else None
        if body.get("kind") != "termux-mcp-fixes" or not isinstance(fixes, list):
            return {"ok": False, "errors": [{"code": "E_FORMAT", "msg": "not a termux-mcp fixes file"}]}
        if hashlib.sha256(json.dumps(fixes, sort_keys=True).encode()).hexdigest() != body.get("sha256"):
            return {"ok": False, "errors": [{"code": "E_TAMPERED", "msg": "checksum mismatch"}]}
        from ..security import get_risk_assessment
        clean = []
        for f in fixes:
            if not (isinstance(f, dict) and isinstance(f.get("steps"), list) and f.get("id") and f.get("code")):
                continue
            if any(get_risk_assessment(str(s))["blocked"] for s in f["steps"]):
                continue
            clean.append(f)
        if not confirmed:
            return {"ok": False, "needs_confirmation": True,
                    "preview": [{"id": f["id"], "code": f["code"], "steps": f["steps"]} for f in clean[:20]],
                    "errors": [{"code": "E_CONFIRM", "msg": "review the steps, then resend with confirmed: true"}]}
        with _lock:
            st = _fixes()
            for f in clean:
                f = dict(f)
                f["tries"], f["ok"] = 0, 0
                st["candidates"].setdefault(f["id"], dict(f, imported=True))
            base.save_state("fix_learned", st)
        return {"ok": True, "imported": len(clean),
                "summary": f"imported {len(clean)} fix(es) as candidates; each is promoted after it works "
                           f"{PROMOTE_AFTER} times here"}
    return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "action: list|export|import"}]}


def candidate_for(code: str, error_text: str = ""):
    fp = fingerprint(error_text, code) if error_text else ""
    for f in _fixes()["candidates"].values():
        if f["code"] == code and (not fp or f["fingerprint"] == fp):
            return {"id": f["id"], "code": code, "risk": "write" if f["risk"] != "safe" else "safe",
                    "desc": f["desc"] + " (learned, untested here)", "steps": list(f["steps"]),
                    **({"verify": f["verify"]} if f.get("verify") else {}),
                    "success_rate": round(f["ok"] / max(f["tries"], 1), 2) * 0.5, "learned": True, "candidate": True}
    return None


PLAN_KEEP = 200
_FILLER = re.compile(r"\b(please|pls|can you|could you|would you|for me|now|just|the|a|an|my|me)\b")


def normalise_request(text: str) -> str:
    t = re.sub(r"[^\w\s:/.@-]", " ", str(text or "").lower())
    t = _FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _plan_key(request: str, etag: str = "") -> str:
    return hashlib.sha1(f"{normalise_request(request)}|{etag}".encode()).hexdigest()[:16]


def _context_etag() -> str:
    import platform
    from . import context
    present = [t for t in context.TOOLS if base.which(t)]
    raw = json.dumps([present, platform.machine(), os.environ.get("TERMUX_VERSION", "")])
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def plan_cache_put(request: str, steps, etag: str = None, verify: str = "") -> dict:
    if not str(request or "").strip():
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "request"}]}
    plan = compact(steps)
    commands = plan["steps"]
    if not commands:
        return {"ok": False, "errors": [{"code": "E_NOTHING", "msg": "no successful steps to cache"}]}
    etag = _context_etag() if etag is None else str(etag)
    key = _plan_key(request, etag)
    with _lock:
        st = base.load_state("plan_cache", {})
        st[key] = {"request": str(request)[:200], "norm": normalise_request(request), "etag": etag,
                   "steps": commands, "verify": verify or plan["verify"], "saved": base.now_iso(),
                   "hits": st.get(key, {}).get("hits", 0)}
        if len(st) > PLAN_KEEP:
            for old in list(st)[:len(st) - PLAN_KEEP]:
                st.pop(old, None)
        base.save_state("plan_cache", st)
    return {"ok": True, "key": key, "steps": len(commands), "summary": f"plan cached ({len(commands)} step(s))"}


def plan_cache_get(request: str, etag: str = None) -> dict:
    st = base.load_state("plan_cache", {})
    etag = _context_etag() if etag is None else str(etag)
    entry = st.get(_plan_key(request, etag))
    if entry is None:
        norm = normalise_request(request)
        stale = next((e for e in reversed(list(st.values())) if e.get("norm") == norm), None)
        if stale:
            return {"ok": True, "hit": False, "stale": True, "steps": stale["steps"], "verify": stale.get("verify", ""),
                    "summary": "a plan exists but the device changed since; plan_replay re-checks it"}
        return {"ok": True, "hit": False, "summary": "no cached plan"}
    return {"ok": True, "hit": True, "key": _plan_key(request, etag), "steps": entry["steps"],
            "verify": entry.get("verify", ""), "hits": entry.get("hits", 0),
            "summary": f"cached plan: {len(entry['steps'])} step(s), replay with plan_replay"}


def plan_replay(request: str = "", steps=None, confirmed: bool = False, task_id: str = "") -> dict:
    from . import accuracy
    from . import _run_digest  # noqa: circular at import time only
    verify = ""
    if not steps:
        got = plan_cache_get(request)
        if not got.get("steps"):
            return {"ok": False, "errors": [{"code": "E_NO_PLAN", "msg": "no cached plan for that request"}]}
        steps, verify = got["steps"], got.get("verify", "")
    check = accuracy.plan_check([{"cmd": s} if isinstance(s, str) else s for s in steps])
    blocking = [e for e in check.get("steps", []) if e.get("blocked") or e.get("problems")
                or (e.get("needs_confirmation") and not confirmed)]
    if blocking:
        return {"ok": False, "checked": check, "errors": [{"code": "E_PLAN_CHECK",
                "msg": f"step {blocking[0]['i'] + 1} would fail or needs confirmation"}]}
    task_id = task_id or ("replay-" + uuid.uuid4().hex[:6])
    results = []
    for s in steps:
        cmd = s if isinstance(s, str) else s.get("cmd", "")
        r = _run_digest({"cmd": cmd, "confirmed": confirmed, "task_id": task_id})
        r.pop("_raw", None)
        results.append({"cmd": cmd, **{k: r.get(k) for k in ("ok", "exit", "summary", "errors") if k in r}})
        if not r.get("ok"):
            return {"ok": False, "results": results, "task_id": task_id,
                    "errors": r.get("errors") or [{"code": "E_STEP", "msg": f"failed: {cmd}"}],
                    "summary": f"stopped at step {len(results)}/{len(steps)}"}
    verified = None
    if verify:
        v = base.sh(verify, timeout=60)
        verified = v.ok
    if request:
        with _lock:
            st = base.load_state("plan_cache", {})
            for e in st.values():
                if e.get("norm") == normalise_request(request):
                    e["hits"] = e.get("hits", 0) + 1
            base.save_state("plan_cache", st)
    return {"ok": verified is not False, "results": results, "verified": verified, "task_id": task_id,
            "summary": f"replayed {len(results)} step(s) with no AI" + ("" if verified is None else
                                                                        f"; verify {'passed' if verified else 'FAILED'}")}


TASK_TYPES = {
    "install": (r"\b(install|pkg|apt|pip|npm|package|dependency|module|library|upgrade)\b",
                ["pkg_ensure", "venv_ensure", "node_ensure", "proot_ensure", "fix_lookup"],
                ["Use pkg_ensure for packages: it installs only what is missing and verifies versions.",
                 "Python packages go in a venv (venv_ensure) when pip reports PEP 668.",
                 "Never use sudo; Termux has no root."]),
    "git": (r"\b(git|repo|repository|clone|commit|push|pull|branch|merge)\b",
            ["git_sync", "run_digest", "project_profile"],
            ["git_sync does fetch+status+fast-forward in one call.",
             "Clone into ~/projects/<name> unless told otherwise."]),
    "files": (r"\b(file|folder|directory|edit|rename|move|copy|delete|find|search|replace|read)\b",
              ["file_find", "file_edit", "out_read", "timeline", "undo_task"],
              ["file_edit snapshots and verifies; use it instead of sed/echo redirects.",
               "file_find searches by name/content with an index; do not ls -R.",
               "Every change is undoable with undo_task."]),
    "services": (r"\b(server|service|bot|port|daemon|start|stop|running|listen|tunnel|expose)\b",
                 ["service_ensure", "service_stop", "port_free", "tunnel", "log_watch"],
                 ["service_ensure starts detached and health-checks the port.",
                  "port_free shows the owner before stopping anything."]),
    "device": (r"\b(battery|wifi|camera|photo|clipboard|share|notification|sms|location|volume|torch|screen)\b",
               ["context_pack", "screen_ocr", "camera_scan", "clipboard_pipe", "share_to", "notify_ask"],
               ["termux-* helpers need Termux:API; they time out after 25 s without it."]),
    "automation": (r"\b(every|when|schedule|cron|daily|hourly|remind|automate|watch|backup)\b",
                   ["cron_ensure", "watch_add", "log_watch", "backup_incremental", "health_watch"],
                   ["cron_ensure is idempotent and captures logs; watch_add for event rules."]),
    "data": (r"\b(sqlite|database|csv|sql|query|table|json|web|url|fetch|download|scrape|page)\b",
             ["db_query", "web_fetch", "out_read"],
             ["db_query returns a table digest; web_fetch returns readable text, not HTML."]),
    "media": (r"\b(video|audio|mp3|mp4|gif|convert|compress|ffmpeg|image|resize)\b",
              ["media_convert", "task_start", "task_status"],
              ["media_convert has presets; long conversions run as a task."]),
    "remote": (r"\b(ssh|remote|server at|vps|host)\b",
               ["ssh_hosts", "ssh_run"],
               ["ssh_run uses saved hosts; it never prompts for a password."]),
}

BASE_RULES = [
    "Answer from tool output only; never guess versions, paths or sizes.",
    "Prefer smart tools (one call, digest back) over raw shell.",
    "On an error digest, try the listed fixes before improvising.",
    "Group independent reads into one batch call.",
]


def classify_task(text: str) -> list:
    t = str(text or "").lower()
    hits = [(name, len(re.findall(rx, t))) for name, (rx, _, _) in TASK_TYPES.items()]
    hits = [h for h in hits if h[1]]
    hits.sort(key=lambda h: -h[1])
    return [h[0] for h in hits[:2]] or ["general"]


def prompt_pack(task_type: str = "", text: str = "") -> dict:
    types = [t for t in re.split(r"[,\s]+", str(task_type or "")) if t in TASK_TYPES] or classify_task(text)
    rules, tools = list(BASE_RULES), ["run_digest", "batch", "context_pack", "tool_search"]
    for t in types:
        if t in TASK_TYPES:
            _, extra_tools, extra_rules = TASK_TYPES[t]
            rules += extra_rules
            tools += [x for x in extra_tools if x not in tools]
    text_out = "\n".join(f"- {r}" for r in rules)
    return {"ok": True, "types": types, "rules": rules, "tools": tools, "text": text_out,
            "chars": len(text_out), "summary": f"{len(rules)} rules for {'+'.join(types)}"}
