import time

from . import accuracy, base, fixgraph

DEFAULT_BUDGET = {"steps": 20, "minutes": 15, "fixes": 3}


def _norm_steps(steps):
    out = []
    for s in steps or []:
        if isinstance(s, str) and s.strip():
            out.append({"cmd": s.strip()})
        elif isinstance(s, dict) and (s.get("cmd") or s.get("tool")):
            out.append(s)
    return out


def _find_plan(goal: str):
    from . import learn
    got = learn.plan_cache_get(goal)
    if got.get("steps"):
        return [{"cmd": c} for c in got["steps"]], got.get("verify", ""), "plan_cache"
    try:
        from ..playbook import match_text, resolve, MIN_CONFIDENCE
        cands = match_text(goal)
        if cands and cands[0]["score"] >= MIN_CONFIDENCE and not cands[0]["missing"]:
            res = resolve(cands[0]["playbook"], cands[0]["inputs"])
            if res.get("ok") if isinstance(res, dict) else False:
                steps = [{"cmd": s["run"]} for s in res.get("steps", []) if s.get("run")]
                verify = next((s.get("verify") for s in reversed(res.get("steps", [])) if s.get("verify")), "")
                if steps:
                    return steps, verify or "", f"playbook:{cands[0]['playbook']}"
    except Exception:
        pass
    return [], "", ""


def _run_step(step: dict, tx: str, confirmed: bool) -> dict:
    from . import _dispatch, _run_digest
    if step.get("cmd"):
        body = _run_digest({"cmd": step["cmd"], "task_id": tx, "confirmed": confirmed,
                            "timeout": step.get("timeout")})
    else:
        params = dict(step.get("params") or {})
        params.setdefault("task_id", tx)
        if confirmed:
            params.setdefault("confirmed", True)
        body = _dispatch(str(step["tool"]), params)
    body.pop("_raw", None)
    return body


def _brief(step: dict, body: dict) -> dict:
    row = {"step": step.get("cmd") or step.get("tool"), "ok": bool(body.get("ok"))}
    for k in ("exit", "summary", "errors", "out_ref"):
        if body.get(k) not in (None, "", []):
            row[k] = body[k]
    return row


def goal_run(goal: str = "", steps=None, verify=None, budget=None, confirmed: bool = False,
             rollback: bool = True, dry_run: bool = False) -> dict:
    limits = dict(DEFAULT_BUDGET)
    if isinstance(budget, dict):
        for k in limits:
            try:
                if budget.get(k) is not None:
                    limits[k] = max(0, int(budget[k]))
            except (TypeError, ValueError):
                pass
    plan = _norm_steps(steps)
    source = "caller"
    verify_list = [v for v in ([verify] if isinstance(verify, str) else (verify or [])) if str(v).strip()]
    if not plan:
        plan, found_verify, source = _find_plan(goal)
        if found_verify and not verify_list:
            verify_list = [found_verify]
    if not plan:
        return {"ok": False, "status": "decision", "decision": "need_plan",
                "errors": [{"code": "E_NO_PLAN", "msg": "no cached plan or playbook for this goal"}],
                "summary": "send steps: [{cmd}|{tool, params}] (and verify) to goal_run"}
    if len(plan) > limits["steps"]:
        return {"ok": False, "status": "decision", "decision": "over_budget",
                "errors": [{"code": "E_BUDGET", "msg": f"{len(plan)} steps > budget {limits['steps']}"}]}

    check = accuracy.plan_check(plan)
    hard = [e for e in check["steps"] if e.get("blocked")]
    confirm = [e for e in check["steps"] if e.get("needs_confirmation")]
    missing = [e for e in check["steps"] if e.get("problems")]
    if hard:
        return {"ok": False, "status": "decision", "decision": "blocked", "checked": check,
                "errors": [{"code": "E_BLOCKED", "msg": f"step {hard[0]['i'] + 1} is blocked by the safety filter"}]}
    if confirm and not confirmed:
        return {"ok": False, "status": "decision", "decision": "confirm", "checked": check,
                "needs_confirmation": True,
                "errors": [{"code": "E_CONFIRM", "msg": f"step {confirm[0]['i'] + 1} is risky; resend with confirmed: true"}]}
    if dry_run:
        return {"ok": not missing, "dry_run": True, "source": source, "plan": plan, "verify": verify_list,
                "checked": check, "summary": check["summary"]}

    tx = accuracy.tx_begin(f"goal: {goal[:60]}" if goal else "goal_run")["tx"]
    started = time.time()
    trail, fixes_used = [], 0

    def out_of_time():
        return limits["minutes"] and time.time() - started > limits["minutes"] * 60

    def stop(decision: str, errors, extra=None):
        body = {"ok": False, "status": "decision", "decision": decision, "tx": tx, "source": source,
                "trail": trail, "errors": errors, "fixes_used": fixes_used,
                "secs": round(time.time() - started, 1)}
        if rollback:
            rb = accuracy.tx_rollback(tx, confirmed=True)
            body["rolled_back"] = rb.get("reverted", 0)
            body["summary"] = f"{decision}: rolled back {rb.get('reverted', 0)} change(s)"
        else:
            body["summary"] = f"{decision}: changes kept in {tx} (tx_rollback to undo)"
        body.update(extra or {})
        return body

    for i, step in enumerate(plan):
        if out_of_time():
            return stop("over_budget", [{"code": "E_BUDGET", "msg": f"over {limits['minutes']} min"}])
        body = _run_step(step, tx, confirmed)
        trail.append(_brief(step, body))
        if body.get("ok"):
            continue
        err = (body.get("errors") or [{"code": "E_STEP", "msg": "step failed"}])[0]
        if err.get("code") in ("E_BLOCKED", "E_POLICY", "E_CAPABILITY", "E_CONFIRM"):
            return stop("blocked", [err])
        repaired = False
        if fixes_used < limits["fixes"] and step.get("cmd"):
            known = fixgraph.lookup(code=err.get("code", ""), subject=err.get("subject", ""), cmd=step["cmd"])
            safe = [f for f in known.get("fixes", []) if f["risk"] == "safe" or confirmed]
            if safe:
                fix = safe[0]
                fixes_used += 1
                applied = fixgraph.apply(fix["id"], err.get("subject", ""), step["cmd"], confirmed=True)
                trail.append({"fix": fix["id"], "ok": bool(applied.get("ok")), "summary": applied.get("summary")})
                retry = _run_step(step, tx, confirmed)
                trail.append(dict(_brief(step, retry), retry=True))
                repaired = bool(retry.get("ok"))
                if not repaired:
                    body = retry
                    err = (retry.get("errors") or [err])[0]
        if not repaired:
            known = fixgraph.lookup(code=err.get("code", ""), subject=err.get("subject", ""),
                                    cmd=step.get("cmd", ""))
            return stop("step_failed", [err], {
                "failed_step": i + 1, "options": [{"id": f["id"], "risk": f["risk"], "desc": f["desc"]}
                                                  for f in known.get("fixes", [])[:3]],
                "hint": "choose a fix (fix_apply), change the plan, or ask the user"})

    verified = []
    for v in verify_list:
        res = base.sh(v, timeout=120)
        verified.append({"cmd": v, "ok": res.ok, "exit": res.exit})
        if not res.ok:
            return stop("verify_failed", [{"code": "E_VERIFY", "msg": f"verify failed: {v}"}],
                        {"verified": verified})
    accuracy.tx_commit(tx)
    try:
        from . import learn
        if goal:
            learn.plan_cache_put(goal, [s["cmd"] for s in plan if s.get("cmd")],
                                 verify=verify_list[0] if verify_list else "")
    except Exception:
        pass
    return {"ok": True, "status": "done", "tx": tx, "source": source, "trail": trail,
            "verified": verified or None, "fixes_used": fixes_used, "secs": round(time.time() - started, 1),
            "summary": f"done: {len(plan)} step(s)" + (f", {fixes_used} auto-fix" if fixes_used else "")
                       + (", verified" if verified else "") + f" (undo: undo_task {tx})"}
