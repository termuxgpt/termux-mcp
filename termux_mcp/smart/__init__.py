import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import (accuracy, base, cache, context, device, digest, envtwin, explain,
               fixgraph, goal, graph, health, intent, intent_tools, learn, meter,
               power, reactor, sandbox, semantic, tasks, timeline)


def _s(**props):
    return {"type": "object", "properties": props}


def _req(schema, *names):
    schema["required"] = list(names)
    return schema


STR = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
ARR = {"type": "array", "items": {"type": "string"}}
OBJ = {"type": "object"}

SMART_TOOL_DEFS = [
    {"name": "run_digest", "category": "shell",
     "description": "Shell command → compact digest {ok,exit,summary,facts,errors,out_ref}. Prefer over run.",
     "inputSchema": _req(_s(cmd=STR, cwd=STR, confirmed=BOOL), "cmd")},
    {"name": "out_read", "category": "shell",
     "description": "More of a stored output: grep, head, tail or lines 'a-b'.",
     "inputSchema": _req(_s(ref=STR, grep=STR, head=INT, tail=INT, lines=STR), "ref")},
    {"name": "context_pack", "category": "context",
     "description": "Environment summary in one call. Send etag back to get {unchanged:true}.",
     "inputSchema": _s(etag=STR, memory_file=STR, cwd=STR)},
    {"name": "batch", "category": "shell",
     "description": "Many independent read-only calls in one round trip: [{tool,params}|{cmd}].",
     "inputSchema": _req(_s(calls={"type": "array", "items": OBJ}, stop_on_error=BOOL), "calls")},
    {"name": "tool_search", "category": "context",
     "description": "Find tools for a need; returns their schemas. Call them via smart.",
     "inputSchema": _req(_s(query=STR, k=INT), "query")},
    {"name": "pkg_ensure", "category": "packages",
     "description": "Install only missing packages (pkg|pip) and return verified versions.",
     "inputSchema": _req(_s(names=ARR, manager={"type": "string", "enum": ["pkg", "pip"]}, task_id=STR), "names")},
    {"name": "file_edit", "category": "files",
     "description": "Safe edit: find/replace, content or append; snapshot + verify + diff.",
     "inputSchema": _req(_s(path=STR, find=STR, replace=STR, content=STR, append=STR, regex=BOOL, count=INT,
                            dry_run=BOOL, task_id=STR), "path")},
    {"name": "service_ensure", "category": "services",
     "description": "Keep a long-running command up (bot, web server). Starts it detached, health-checks the "
                    "port, returns {up, pid, url} or the log tail and error codes.",
     "inputSchema": _req(_s(name=STR, cmd=STR, port=INT, cwd=STR, restart=BOOL, wait=INT), "name")},
    {"name": "service_stop", "category": "services", "description": "Stop a managed service.",
     "inputSchema": _req(_s(name=STR), "name")},
    {"name": "port_free", "category": "services",
     "description": "Show what holds a TCP port; with confirmed: true stop it and verify the port is free.",
     "inputSchema": _req(_s(port=INT, confirmed=BOOL), "port")},
    {"name": "project_profile", "category": "projects",
     "description": "Detect a project's language, install/run/test commands and README commands. Cached.",
     "inputSchema": _s(path=STR)},
    {"name": "project_run", "category": "projects",
     "description": "Install dependencies and run (or test) a project in one call; failures come back with "
                    "error codes and known fixes.",
     "inputSchema": _s(path=STR, install=BOOL, test=BOOL, timeout=INT, confirmed=BOOL)},
    {"name": "storage_clean", "category": "storage",
     "description": "Measure then free space: apt_cache, pip_cache, npm_cache, tmp, pycache, old_logs, trash. "
                    "dry_run (default) reports reclaimable size; logs/pycache go to trash (undoable).",
     "inputSchema": _s(categories=ARR, dry_run=BOOL, confirmed=BOOL, task_id=STR)},
    {"name": "git_sync", "category": "git",
     "description": "Fetch + status + fast-forward pull in one call: branch, ahead/behind, dirty files.",
     "inputSchema": _s(path=STR, pull=BOOL)},
    {"name": "intent", "category": "routing",
     "description": "Route a request locally: local|choose|ai + candidate tool calls.",
     "inputSchema": _req(_s(text=STR), "text")},
    {"name": "fix_lookup", "category": "repair",
     "description": "Tested fixes for an error code or text, ranked by success rate.",
     "inputSchema": _s(error=STR, code=STR, cmd=STR, subject=STR)},
    {"name": "fix_apply", "category": "repair",
     "description": "Apply a FixGraph fix by id and verify it. Non-safe fixes need confirmed: true.",
     "inputSchema": _req(_s(id=STR, subject=STR, cmd=STR, confirmed=BOOL, cwd=STR), "id")},
    {"name": "graph_query", "category": "inventory",
     "description": "Ask the device graph: 'is node installed', 'version of python', 'my projects', "
                    "'services', 'cron jobs' — no probing.",
     "inputSchema": _req(_s(q=STR), "q")},
    {"name": "graph_refresh", "category": "inventory",
     "description": "Rebuild the device graph (packages, commands, projects, services, cron).",
     "inputSchema": _s(parts=ARR)},
    {"name": "shell_lint", "category": "safety",
     "description": "Check and auto-fix a command before running (brace expansion, cd chains, missing -y, "
                    "sudo, PEP 668, interactive programs, risk).",
     "inputSchema": _req(_s(cmd=STR), "cmd")},
    {"name": "plan_check", "category": "safety",
     "description": "Dry-run steps [{cmd}]: missing cmds/files/pkgs, busy ports, blocked.",
     "inputSchema": _req(_s(steps={"type": "array", "items": OBJ}), "steps")},
    {"name": "tx_begin", "category": "safety",
     "description": "Open a transaction; pass the returned task_id to writes so tx_rollback undoes them all.",
     "inputSchema": _s(title=STR)},
    {"name": "tx_status", "category": "safety", "description": "Changes recorded under a transaction.",
     "inputSchema": _req(_s(tx=STR), "tx")},
    {"name": "tx_commit", "category": "safety", "description": "Mark a transaction as kept.",
     "inputSchema": _req(_s(tx=STR), "tx")},
    {"name": "tx_rollback", "category": "safety",
     "description": "Undo every change of a transaction (needs confirmed: true).",
     "inputSchema": _req(_s(tx=STR, confirmed=BOOL), "tx")},
    {"name": "answer_check", "category": "safety",
     "description": "Flag claims in a final answer that no tool result backs.",
     "inputSchema": _req(_s(answer=STR, evidence=ARR, actions=ARR), "answer")},
    {"name": "watch_add", "category": "automation",
     "description": "Event reactor: when {battery_below, charging, wifi, at 'HH:MM', every_min, file_changed, "
                    "port_down, storage_below_gb} → action {cmd} and/or {notify}. Runs forever, 0 tokens.",
     "inputSchema": _req(_s(when=OBJ, action=OBJ, name=STR, confirmed=BOOL), "when", "action")},
    {"name": "watch_list", "category": "automation", "description": "List event-reactor rules.",
     "inputSchema": _s()},
    {"name": "watch_remove", "category": "automation", "description": "Delete an event-reactor rule.",
     "inputSchema": _req(_s(id=STR), "id")},
    {"name": "watch_toggle", "category": "automation", "description": "Pause or resume a rule.",
     "inputSchema": _req(_s(id=STR, enabled=BOOL), "id")},
    {"name": "sandbox_run", "category": "safety",
     "description": "Run a command on a throw-away copy of a folder and report exactly which files it would "
                    "add/change/delete (with diffs). sandbox_apply keeps the result.",
     "inputSchema": _req(_s(cmd=STR, path=STR, timeout=INT), "cmd")},
    {"name": "sandbox_apply", "category": "safety", "description": "Apply a sandbox's changes (undoable).",
     "inputSchema": _req(_s(sandbox=STR, confirmed=BOOL, task_id=STR), "sandbox")},
    {"name": "sandbox_discard", "category": "safety", "description": "Throw a sandbox away.",
     "inputSchema": _req(_s(sandbox=STR), "sandbox")},
    {"name": "explain_cmd", "category": "help",
     "description": "Explain a shell command part by part (commands, flags, pipes, redirects, risk) with no "
                    "model.",
     "inputSchema": _req(_s(cmd=STR), "cmd")},
    {"name": "snapshot_env", "category": "backup",
     "description": "Save a portable twin of this Termux: packages, pip/npm globals, dotfiles, cron, services.",
     "inputSchema": _s(name=STR)},
    {"name": "restore_env", "category": "backup",
     "description": "Restore a twin on this phone. dry_run (default) shows the plan.",
     "inputSchema": _req(_s(file=STR, dry_run=BOOL, confirmed=BOOL, parts=ARR), "file")},
    {"name": "screen_ocr", "category": "media",
     "description": "Read text (and QR/barcodes) from the screen or an image on the device — no AI vision.",
     "inputSchema": _s(image=STR, lang=STR)},
    {"name": "camera_scan", "category": "media",
     "description": "Take a photo and read QR/barcodes and text from it on the device.",
     "inputSchema": _s(camera=INT, lang=STR, image=STR)},
    {"name": "notify_ask", "category": "device",
     "description": "Ask the user through an Android notification with up to 3 buttons and wait for the tap.",
     "inputSchema": _req(_s(question=STR, options=ARR, timeout=INT, title=STR), "question")},
    {"name": "recipe_share", "category": "automation",
     "description": "Share playbooks as signed capsules: action export (id) | preview (file) | import (file).",
     "inputSchema": _req(_s(action={"type": "string", "enum": ["export", "preview", "import"]}, id=STR, file=STR,
                            sign=BOOL, confirmed=BOOL), "action")},
    {"name": "cost_meter", "category": "context",
     "description": "Tokens and AI calls saved by digests and local routing (overall or per task_id).",
     "inputSchema": _s(task_id=STR, reset=BOOL)},
    {"name": "health_watch", "category": "diagnose",
     "description": "Device health report: action run (check now) | report (last stored, no probing) | "
                    "enable (daily at 'at') | disable.",
     "inputSchema": _s(action={"type": "string", "enum": ["run", "report", "enable", "disable"]}, at=STR)},
    {"name": "task_start", "category": "tasks",
     "description": "Run any smart tool as a background task (long builds, backups, twins). Returns task id.",
     "inputSchema": _req(_s(tool=STR, params=OBJ), "tool")},
    {"name": "task_status", "category": "tasks", "description": "Status (and result when done) of a task.",
     "inputSchema": _req(_s(task=STR), "task")},
    {"name": "task_list", "category": "tasks", "description": "Recent background tasks.",
     "inputSchema": _s()},
    {"name": "digest_text", "category": "shell",
     "description": "Digest output a client already has (e.g. a streamed run): stores the full text and "
                    "returns the compact digest with out_ref.",
     "inputSchema": _req(_s(cmd=STR, text=STR, exit=INT), "text")},
    {"name": "learn_propose", "category": "learning",
     "description": "Turn a finished task into a replayable playbook draft: failed/exploratory steps dropped, "
                    "values become slots, a verify step kept. From steps [{cmd,ok}] or a task_id.",
     "inputSchema": _s(steps={"type": "array", "items": OBJ}, task_id=STR, title=STR, request=STR)},
    {"name": "learn_save", "category": "learning",
     "description": "Save a learn_propose draft as a playbook; the phrase then replays it with no AI.",
     "inputSchema": _req(_s(draft_id=STR, title=STR, phrases=ARR, overwrite=BOOL), "draft_id")},
    {"name": "fix_learn", "category": "repair",
     "description": "Record a fix that worked for an error FixGraph did not know; promoted after 2 successes.",
     "inputSchema": _req(_s(error=STR, code=STR, fix=ARR, verify=STR, worked=BOOL, desc=STR), "fix")},
    {"name": "fix_share", "category": "repair",
     "description": "Learned fixes: action list | export (file) | import (file, confirmed).",
     "inputSchema": _req(_s(action={"type": "string", "enum": ["list", "export", "import"]}, file=STR,
                            confirmed=BOOL), "action")},
    {"name": "plan_cache_get", "category": "learning",
     "description": "Cached plan for a request on this device (0 AI if hit).",
     "inputSchema": _req(_s(request=STR), "request")},
    {"name": "plan_cache_put", "category": "learning",
     "description": "Cache the steps that fulfilled a request so the same request replays with no AI.",
     "inputSchema": _req(_s(request=STR, steps={"type": "array", "items": OBJ}, verify=STR), "request", "steps")},
    {"name": "plan_replay", "category": "learning",
     "description": "Replay a cached plan (or given steps): plan_check first, stop at the first failure.",
     "inputSchema": _s(request=STR, steps={"type": "array", "items": OBJ}, confirmed=BOOL, task_id=STR)},
    {"name": "prompt_pack", "category": "context",
     "description": "Only the rules and tools a task type needs (install|git|files|services|device|automation|"
                    "data|media|remote), or classify from text.",
     "inputSchema": _s(task_type=STR, text=STR)},
    {"name": "goal_run", "category": "autonomy",
     "description": "Finish a goal server-side: plan_check → steps in one transaction → one auto-fix per failure "
                    "→ verify → commit, or roll back and return a decision. Budget: {steps, minutes, fixes}.",
     "inputSchema": _s(goal=STR, steps={"type": "array", "items": OBJ}, verify=ARR, budget=OBJ,
                       confirmed=BOOL, rollback=BOOL, dry_run=BOOL)},
    {"name": "policy_get", "category": "safety", "description": "The device policy (paths, tools, network, pip).",
     "inputSchema": _s()},
    {"name": "policy_set", "category": "safety",
     "description": "Change the device policy: deny_paths, allow_paths, deny_tools, network, pip "
                    "(any|venv_only), sandbox_unknown_scripts. Loosening needs on-device approval.",
     "inputSchema": _req(_s(policy=OBJ, replace=BOOL, confirmed=BOOL), "policy")},
    {"name": "cap_issue", "category": "safety",
     "description": "Issue a scoped, expiring token for an external MCP client: tools, paths, read_only, ttl_min.",
     "inputSchema": _s(tools=ARR, paths=ARR, read_only=BOOL, ttl_min=INT, label=STR, confirmed=BOOL)},
    {"name": "cap_list", "category": "safety", "description": "Capability tokens and their scopes.",
     "inputSchema": _s()},
    {"name": "cap_revoke", "category": "safety", "description": "Revoke a capability token by id (or 'all').",
     "inputSchema": _req(_s(id=STR), "id")},
    {"name": "timeline", "category": "safety",
     "description": "Changes grouped by task, newest first — any of them can be undone with undo_task.",
     "inputSchema": _s(limit=INT)},
    {"name": "undo_task", "category": "safety",
     "description": "Undo every change of one past task (warns if later tasks touched the same files).",
     "inputSchema": _req(_s(task=STR, confirmed=BOOL), "task")},
    {"name": "metrics", "category": "diagnose",
     "description": "Per-tool latency (p50/p95), error rates, error codes and fix success rates.",
     "inputSchema": _s(reset=BOOL)},
    {"name": "venv_ensure", "category": "packages",
     "description": "Project Python venv (.venv) + requirements, reinstalled only when they change.",
     "inputSchema": _req(_s(path=STR, python=STR, requirements=STR), "path")},
    {"name": "node_ensure", "category": "packages",
     "description": "Node installed (optionally a major version / lts) and a project's deps up to date.",
     "inputSchema": _s(path=STR, version=STR, confirmed=BOOL)},
    {"name": "proot_ensure", "category": "packages",
     "description": "A proot-distro Linux (debian, ubuntu, …) for packages Termux lacks.",
     "inputSchema": _s(distro=STR, confirmed=BOOL)},
    {"name": "proot_run", "category": "packages", "description": "Run a command inside a proot distro; digest back.",
     "inputSchema": _req(_s(distro=STR, cmd=STR, timeout=INT, confirmed=BOOL), "cmd")},
    {"name": "cron_ensure", "category": "automation",
     "description": "Idempotent cron job with log capture and verification: action ensure|remove|list.",
     "inputSchema": _s(name=STR, spec=STR, cmd=STR, action={"type": "string", "enum": ["ensure", "remove", "list"]},
                       confirmed=BOOL)},
    {"name": "tunnel", "category": "services",
     "description": "Expose a local port with a time-limited public URL (cloudflared or ssh): action open|list|stop.",
     "inputSchema": _s(port=INT, action={"type": "string", "enum": ["open", "list", "stop"]}, minutes=INT,
                       provider=STR, id=STR, confirmed=BOOL)},
    {"name": "web_fetch", "category": "network",
     "description": "Fetch a URL as readable text (article/main or a CSS selector: tag, #id, .class); JSON "
                    "pretty-printed. Long pages → out_ref.",
     "inputSchema": _req(_s(url=STR, selector=STR, max_chars=INT), "url")},
    {"name": "db_query", "category": "data",
     "description": "SQL on an SQLite file or a CSV (table t); read-only unless write+confirmed; table digest.",
     "inputSchema": _req(_s(path=STR, sql=STR, limit=INT, write=BOOL, confirmed=BOOL), "path", "sql")},
    {"name": "log_watch", "category": "automation",
     "description": "Regex over a log: action scan (now) | watch (reactor fires on new matches: notify/cmd) | stop.",
     "inputSchema": _s(path=STR, pattern=STR, action={"type": "string", "enum": ["scan", "watch", "stop"]},
                       notify=STR, cmd=STR, name=STR, id=STR, confirmed=BOOL)},
    {"name": "file_find", "category": "files",
     "description": "Indexed file search by name (or glob) or content, with ext / newer_than_days / "
                    "larger_than_mb filters.",
     "inputSchema": _s(query=STR, path=STR, kind={"type": "string", "enum": ["name", "content"]}, ext=STR,
                       newer_than_days={"type": "number"}, larger_than_mb={"type": "number"}, limit=INT,
                       refresh=BOOL)},
    {"name": "backup_incremental", "category": "backup",
     "description": "Incremental, deduplicated backups: action backup|list|verify|restore. Encrypted with "
                    "restic when a password is given.",
     "inputSchema": _s(action={"type": "string", "enum": ["backup", "list", "verify", "restore"]}, src=STR,
                       dest=STR, snapshot=STR, target=STR, password=STR, full=BOOL, confirmed=BOOL)},
    {"name": "clipboard_pipe", "category": "device",
     "description": "Android clipboard: direction get | set (text or file) | to_file (file).",
     "inputSchema": _s(direction={"type": "string", "enum": ["get", "set", "to_file"]}, text=STR, file=STR,
                       max_chars=INT)},
    {"name": "share_to", "category": "device",
     "description": "Open the Android share sheet for a file or text, or open a URL.",
     "inputSchema": _s(path=STR, text=STR, url=STR, action=STR, title=STR)},
    {"name": "media_convert", "category": "media",
     "description": "ffmpeg presets: audio, compress, mp4, gif, thumbnail, resize, trim; background: true runs "
                    "it as a task, action status shows the percent.",
     "inputSchema": _s(input=STR, output=STR, preset=STR, start=STR, duration=STR, crf=INT, width=INT, fps=INT,
                       overwrite=BOOL, action={"type": "string", "enum": ["convert", "status"]}, background=BOOL)},
    {"name": "ssh_hosts", "category": "remote", "description": "Saved SSH hosts: action list|add|remove.",
     "inputSchema": _s(action={"type": "string", "enum": ["list", "add", "remove"]}, name=STR, host=STR,
                       user=STR, port=INT, key=STR)},
    {"name": "ssh_run", "category": "remote",
     "description": "Run a command on a saved host (or user@host) with key auth; digest back.",
     "inputSchema": _req(_s(host=STR, cmd=STR, timeout=INT, confirmed=BOOL), "host", "cmd")},
]

CORE = ("run_digest", "out_read", "context_pack", "batch", "tool_search")

META_DEF = {
    "name": "smart", "category": "meta",
    "description": "Call any smart tool: {tool, params}. Schemas via tool_search. E.g. goal_run, service_ensure, "
                   "venv_ensure, cron_ensure, web_fetch, file_find, db_query, tx_begin, undo_task.",
    "inputSchema": {"type": "object", "properties": {"tool": {"type": "string"},
                                                     "params": {"type": "object"}},
                    "required": ["tool"]},
}

SMART_TOOLS = frozenset([d["name"] for d in SMART_TOOL_DEFS] + ["smart"])
SMART_CATEGORIES = {d["name"]: d["category"] for d in SMART_TOOL_DEFS + [META_DEF]}


def _mcp(d):
    return {k: v for k, v in d.items() if k != "category"}


MCP_DEFS = [_mcp(d) for d in SMART_TOOL_DEFS if d["name"] in CORE] + [_mcp(META_DEF)]
ALL_MCP_DEFS = [_mcp(d) for d in SMART_TOOL_DEFS]

READ_ONLY_BATCH = {"out_read", "context_pack", "project_profile", "graph_query", "shell_lint", "plan_check",
                   "explain_cmd", "fix_lookup", "intent", "cost_meter", "watch_list", "task_status",
                   "tx_status", "plan_cache_get", "prompt_pack", "policy_get", "timeline", "metrics",
                   "file_find", "web_fetch", "cap_list"}


def _run_digest(p: dict) -> dict:
    cmd = str(p.get("cmd", "")).strip()
    if not cmd:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "cmd required"}]}
    lint = None
    if base.boolish(p.get("lint"), True):
        lint = accuracy.shell_lint(cmd)
        if lint["changed"] and not any(i["code"] == "L_QUOTES" for i in lint["issues"]):
            cmd = lint["cmd"]
        if any(i["code"] == "L_INTERACTIVE" for i in lint["issues"]):
            return {"ok": False, "errors": [{"code": "E_INTERACTIVE", "msg": "needs a terminal: use terminal_open"}],
                    "cmd": cmd}
    confirmed = base.boolish(p.get("confirmed"))
    from .. import kernel
    task_id = str(p.get("task_id") or "")
    decision = kernel.gate(cmd, confirmed=confirmed, tool="run_digest", cwd=p.get("cwd") or "")
    if not decision["allow"]:
        body = {"ok": False, "exit": 126, "cmd": cmd,
                "errors": [{"code": decision["code"], "msg": decision["message"]}]}
        if decision["status"] == "confirm":
            body["needs_confirmation"] = True
            body["risk_level"] = decision["risk_level"]
        return body
    confirmed = True
    sandboxed = _sandbox_first(cmd, p)
    if sandboxed is not None:
        return sandboxed
    kernel.prepare(cmd, task_id, echo=False, record=False)
    res = base.sh(cmd, timeout=p.get("timeout"), cwd=p.get("cwd") or None, confirmed=confirmed)
    body = digest.from_shell(res)
    body["cmd"] = cmd
    if lint and lint["changed"]:
        body["linted"] = [i["code"] for i in lint["issues"]]
    if not body["ok"] and body.get("errors"):
        err = body["errors"][0]
        fixgraph.note_failure(err["code"], cmd)
        known = fixgraph.lookup(code=err["code"], subject=err.get("subject", ""), cmd=cmd, error_text=res.text)
        if known["fixes"]:
            body["fixes"] = [{"id": f["id"], "risk": f["risk"], "desc": f["desc"], "steps": f["steps"]}
                             for f in known["fixes"][:2]]
    if re.match(r"^\s*(pkg|apt|pip3?|npm)\s+(install|uninstall|remove|upgrade)", cmd):
        cache.invalidate("pkg", "graph", "context")
    kernel.record_action(task_id, cmd, bool(body.get("ok")), tool="run_digest")
    body["_raw"] = len(res.out) + len(res.err)
    return body


_SCRIPT = re.compile(r"^\s*(?:(?:bash|sh|zsh|dash|python3?|node|perl|ruby|php)\s+(?:-\w+\s+)*([^\s;&|]+)"
                     r"|(\./[^\s;&|]+))")


def _sandbox_first(cmd: str, p: dict):
    from .. import policy
    if not policy.get().get("sandbox_unknown_scripts"):
        return None
    m = _SCRIPT.match(cmd)
    if not m:
        return None
    import hashlib
    import os
    token = m.group(1) or m.group(2)
    cwd = p.get("cwd") or os.getcwd()
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(token)))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            sha = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
    trusted = base.load_state("trusted_scripts", {})
    if trusted.get(sha):
        return None
    if base.boolish(p.get("confirmed")) and p.get("trust_script", True):
        trusted[sha] = {"path": path, "at": base.now_iso()}
        base.save_state("trusted_scripts", dict(list(trusted.items())[-500:]))
        return None
    report = sandbox.run(cmd, os.path.dirname(path), int(p.get("timeout") or 120))
    report["ok"] = False
    report["needs_confirmation"] = True
    report.setdefault("errors", []).insert(0, {"code": "E_SANDBOXED",
                                               "msg": "unknown script: ran in a sandbox copy first; review the "
                                                      "changes, then resend with confirmed: true to run it for real"})
    return report


NATIVE_DRY_RUN = {"file_edit", "storage_clean", "restore_env", "goal_run"}
ADMIN_TOOLS = {"policy_set", "cap_issue", "cap_revoke"}


def _dry_run(name: str, p: dict) -> dict:
    from .. import kernel
    params = {k: v for k, v in p.items() if k != "dry_run"}
    body = {"ok": True, "dry_run": True, "tool": name, "params": params}
    if name in ("run_digest", "proot_run", "ssh_run") and params.get("cmd"):
        lint = accuracy.shell_lint(str(params["cmd"]))
        check = accuracy.plan_check([{"cmd": lint["cmd"]}])
        decision = kernel.gate(lint["cmd"], confirmed=base.boolish(params.get("confirmed")), tool=name,
                               spend_approval=False)
        from ..safety import write_targets
        body.update(cmd=lint["cmd"], issues=lint["issues"], problems=check["steps"][0].get("problems", []),
                    decision=decision["status"], writes=write_targets(lint["cmd"], include_removals=True)[:20])
        body["ok"] = decision["status"] in ("ok", "confirm") and not body["problems"]
        body["summary"] = f"would run: {lint['cmd'][:80]} ({decision['status']})"
        return body
    if name == "cron_ensure" and params.get("cmd"):
        body["summary"] = f"would schedule '{params.get('spec')} {params['cmd']}' as {params.get('name')}"
    elif name == "pkg_ensure":
        names = params.get("names") or []
        names = [names] if isinstance(names, str) else names
        missing = [n for n in names if not base.which(str(n))]
        body.update(missing_commands=missing, summary=f"would install what is missing of {', '.join(map(str, names))}")
    elif name == "tx_rollback":
        body.update(accuracy.tx_status(str(params.get("tx", ""))), dry_run=True)
        body["summary"] = f"would roll back {body.get('changes', 0)} change(s)"
    elif name == "undo_task":
        preview = timeline.undo_task(str(params.get("task", "")), confirmed=False)
        body.update({k: preview[k] for k in ("changes", "files", "conflicts") if k in preview})
        body["summary"] = f"would undo {preview.get('changes', 0)} change(s)"
    elif name == "sandbox_apply":
        body["summary"] = "would copy the sandbox's changes back (undoable)"
    elif name == "plan_replay":
        steps = params.get("steps") or learn.plan_cache_get(str(params.get("request", ""))).get("steps") or []
        body.update(checked=accuracy.plan_check([{"cmd": c} if isinstance(c, str) else c for c in steps]),
                    steps=steps, summary=f"would replay {len(steps)} step(s)")
    else:
        body["summary"] = f"would call {name} with {', '.join(sorted(params)) or 'no parameters'}"
    for key in kernel.PATH_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value:
            from .. import policy
            reason = policy.check_path(value, write=True)
            if reason:
                body.update(ok=False, errors=[{"code": "E_POLICY", "msg": reason}])
    return body


def _batch(p: dict) -> dict:
    calls = p.get("calls") or []
    if not isinstance(calls, list) or not calls:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "calls: [{tool, params}|{cmd}]"}]}
    calls = calls[:12]
    stop = base.boolish(p.get("stop_on_error"))

    def one(call):
        if not isinstance(call, dict):
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "each call must be an object"}]}
        if call.get("cmd"):
            lint = accuracy.shell_lint(call["cmd"])
            if lint["blocked"] or lint["needs_confirmation"]:
                return {"ok": False, "cmd": call["cmd"],
                        "errors": [{"code": "E_CONFIRM", "msg": "batch only runs safe commands"}]}
            return _run_digest({"cmd": call["cmd"], "timeout": call.get("timeout", 60)})
        tool = call.get("tool")
        from .. import kernel
        refusal = kernel.authorize_tool(str(tool), call.get("params") or {})
        if refusal:
            return {"ok": False, "errors": [{"code": "E_CAPABILITY", "msg": refusal}]}
        if tool not in SMART_TOOLS or tool in ("batch", "task_start"):
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"{tool} not allowed in batch"}]}
        if tool not in READ_ONLY_BATCH:
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"{tool} changes state; call it directly"}]}
        return _dispatch(tool, call.get("params") or {})

    results = []
    if stop:
        for call in calls:
            r = one(call)
            results.append(r)
            if not r.get("ok"):
                break
    else:
        from .. import kernel
        principal = kernel.current_principal()
        with ThreadPoolExecutor(max_workers=min(6, len(calls))) as pool:
            results = list(pool.map(lambda c: kernel.run_as(principal, one, c), calls))
    raw = 0
    for r in results:
        raw += r.pop("_raw", 0) if isinstance(r, dict) else 0
    return {"ok": all(r.get("ok") for r in results), "results": results, "_raw": raw,
            "summary": f"{sum(1 for r in results if r.get('ok'))}/{len(results)} ok"}


def _catalog():
    try:
        from ..tools_schema import build_catalog
        from .. import mcp_core
        return build_catalog(list(mcp_core.NATIVE_TOOL_DEFS) + ALL_MCP_DEFS)
    except Exception:
        return [{"name": d["name"], "desc": d["description"], "params": "", "category": d["category"]}
                for d in SMART_TOOL_DEFS]


def _schemas():
    out = {d["name"]: d for d in ALL_MCP_DEFS}
    try:
        from ..tools_schema import OPENAI_TOOLS
        for e in OPENAI_TOOLS:
            fn = e.get("function", {})
            out.setdefault(fn.get("name"), {"name": fn.get("name"), "description": fn.get("description", ""),
                                            "inputSchema": fn.get("parameters", {})})
        from .. import mcp_core
        for d in mcp_core.NATIVE_TOOL_DEFS:
            out.setdefault(d["name"], d)
    except Exception:
        pass
    return out


_SYN = {"install": "pkg package", "delete": "remove trash", "remove": "delete", "space": "storage clean disk",
        "disk": "storage space", "server": "service port", "bot": "service", "kill": "stop port process",
        "undo": "rollback revert changes", "revert": "undo rollback", "schedule": "cron watch",
        "when": "watch", "photo": "camera", "picture": "camera", "text": "ocr", "qr": "camera scan",
        "battery": "battery watch", "backup": "snapshot backup", "move": "snapshot restore twin",
        "explain": "explain", "error": "fix", "broken": "fix doctor health", "slow": "health process"}


def _tool_search(p: dict) -> dict:
    query = str(p.get("query", "")).lower()
    k = max(1, min(int(p.get("k") or 3), 8))
    words = set(re.findall(r"[a-z0-9]+", query))
    for w in list(words):
        words.update(_SYN.get(w, "").split())
    scored = []
    for row in _catalog():
        hay = f"{row['name'].replace('_', ' ')} {row.get('desc', '')} {row.get('category', '')}".lower()
        score = sum(3 if w in row["name"] else 1 for w in words if w and w in hay)
        if score:
            scored.append((score, row["name"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    schemas = _schemas()
    hits = [schemas[n] for _, n in scored[:k] if n in schemas]
    return {"ok": True, "tools": hits, "summary": ", ".join(h["name"] for h in hits) or "no match"}


def _recipe_share(p: dict) -> dict:
    from .. import capsule
    action = p.get("action")
    if action == "export":
        return capsule.export_capsule(str(p.get("id", "")), sign_it=base.boolish(p.get("sign")))
    if action == "preview":
        return capsule.preview_capsule(str(p.get("file", "")))
    if action == "import":
        return capsule.import_capsule(str(p.get("file", "")), confirmed=base.boolish(p.get("confirmed")))
    return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "action: export|preview|import"}]}


def _dispatch(name: str, p: dict) -> dict:
    b = base.boolish
    from .. import kernel, policy
    if name != "smart":
        refusal = kernel.authorize_tool(name, p)
        if refusal:
            code = "E_CAPABILITY" if kernel.current_principal() is not None else "E_POLICY"
            return {"ok": False, "errors": [{"code": code, "msg": refusal}]}
        write = kernel.is_write_call(name, p)
        for key in kernel.PATH_KEYS:
            value = p.get(key)
            if isinstance(value, str) and value and name not in ("run_digest", "proot_run", "ssh_run"):
                reason = policy.check_path(value, write=write and key not in ("cwd", "src", "input", "image"),
                                           cwd=str(p.get("cwd") or ""))
                if reason:
                    return {"ok": False, "errors": [{"code": "E_POLICY", "msg": reason}]}
        if b(p.get("dry_run")) and name in kernel.WRITE_TOOLS and name not in NATIVE_DRY_RUN:
            return _dry_run(name, p)
    if name == "smart":
        inner = str(p.get("tool", ""))
        if inner == "smart" or inner not in SMART_TOOLS:
            hint = _tool_search({"query": inner.replace("_", " "), "k": 3})
            return {"ok": False, "errors": [{"code": "E_UNKNOWN_TOOL", "msg": f"no smart tool {inner}"}],
                    "did_you_mean": [t["name"] for t in hint["tools"]]}
        return _dispatch(inner, p.get("params") or {})
    if name == "run_digest":
        return _run_digest(p)
    if name == "digest_text":
        text = str(p.get("text", ""))
        exit_code = p.get("exit")
        if exit_code is None:
            exit_code = 1 if digest.classify(text, 0) else 0
        body = digest.make(str(p.get("cmd", "")), int(exit_code), text)
        if not body["ok"] and body.get("errors"):
            err = body["errors"][0]
            known = fixgraph.lookup(code=err["code"], subject=err.get("subject", ""), cmd=str(p.get("cmd", "")))
            if known["fixes"]:
                body["fixes"] = [{"id": f["id"], "risk": f["risk"], "desc": f["desc"]} for f in known["fixes"][:2]]
        body["_raw"] = len(text)
        return body
    if name == "out_read":
        return digest.read_output(p.get("ref", ""), grep=p.get("grep", ""), head=int(p.get("head") or 0),
                                  tail=int(p.get("tail") or 0), lines=p.get("lines", ""))
    if name == "context_pack":
        return context.pack(p.get("etag", ""), p.get("memory_file", ""), p.get("cwd", ""))
    if name == "batch":
        return _batch(p)
    if name == "tool_search":
        return _tool_search(p)
    if name == "pkg_ensure":
        return intent_tools.pkg_ensure(p.get("names"), p.get("manager", "pkg"), p.get("task_id", ""))
    if name == "file_edit":
        return intent_tools.file_edit(p.get("path", ""), p.get("find", ""), p.get("replace", ""),
                                      p.get("content"), int(p.get("count") or 0), b(p.get("regex")),
                                      p.get("append", ""), p.get("task_id", ""), b(p.get("dry_run")))
    if name == "service_ensure":
        return intent_tools.service_ensure(p.get("name", ""), p.get("cmd", ""), int(p.get("port") or 0),
                                           p.get("cwd", ""), b(p.get("restart")), int(p.get("wait") or 15))
    if name == "service_stop":
        return intent_tools.service_stop(p.get("name", ""))
    if name == "port_free":
        return intent_tools.port_free(p.get("port"), b(p.get("confirmed")))
    if name == "project_profile":
        return intent_tools.project_profile(p.get("path", ""))
    if name == "project_run":
        return intent_tools.project_run(p.get("path", ""), b(p.get("install"), True), b(p.get("test")),
                                        int(p.get("timeout") or 300), b(p.get("confirmed")))
    if name == "storage_clean":
        return intent_tools.storage_clean(p.get("categories"), b(p.get("dry_run"), True), b(p.get("confirmed")),
                                          p.get("task_id", ""))
    if name == "git_sync":
        return intent_tools.git_sync(p.get("path", ""), b(p.get("pull"), True))
    if name == "intent":
        return intent.route(p.get("text", ""))
    if name == "fix_lookup":
        return fixgraph.lookup(p.get("error", ""), p.get("code", ""), p.get("cmd", ""), p.get("subject", ""))
    if name == "fix_apply":
        return fixgraph.apply(p.get("id", ""), p.get("subject", ""), p.get("cmd", ""), b(p.get("confirmed")),
                              p.get("cwd", ""))
    if name == "graph_query":
        return graph.query(p.get("q", ""))
    if name == "graph_refresh":
        parts = p.get("parts") or ("packages", "commands", "projects", "services", "cron")
        return graph.refresh(tuple(parts))
    if name == "shell_lint":
        return accuracy.shell_lint(p.get("cmd", ""))
    if name == "plan_check":
        return accuracy.plan_check(p.get("steps") or [])
    if name == "tx_begin":
        return accuracy.tx_begin(p.get("title", ""))
    if name == "tx_status":
        return accuracy.tx_status(p.get("tx", ""))
    if name == "tx_commit":
        return accuracy.tx_commit(p.get("tx", ""))
    if name == "tx_rollback":
        return accuracy.tx_rollback(p.get("tx", ""), b(p.get("confirmed")))
    if name == "answer_check":
        return accuracy.answer_check(p.get("answer", ""), p.get("evidence"), p.get("actions"))
    if name == "watch_add":
        return reactor.add(p.get("when") or {}, p.get("action") or {}, p.get("name", ""), b(p.get("confirmed")))
    if name == "watch_list":
        reactor.ensure_running()
        return reactor.list_rules()
    if name == "watch_remove":
        return reactor.remove(p.get("id", ""))
    if name == "watch_toggle":
        return reactor.set_enabled(p.get("id", ""), b(p.get("enabled"), True))
    if name == "sandbox_run":
        return sandbox.run(p.get("cmd", ""), p.get("path", ""), int(p.get("timeout") or 120))
    if name == "sandbox_apply":
        return sandbox.apply(p.get("sandbox", ""), b(p.get("confirmed")), p.get("task_id", ""))
    if name == "sandbox_discard":
        return sandbox.discard(p.get("sandbox", ""))
    if name == "explain_cmd":
        return explain.explain(p.get("cmd", ""))
    if name == "snapshot_env":
        return envtwin.snapshot(p.get("name", ""))
    if name == "restore_env":
        return envtwin.restore(p.get("file", ""), b(p.get("dry_run"), True), b(p.get("confirmed")), p.get("parts"))
    if name == "screen_ocr":
        return device.screen_ocr(p.get("image", ""), p.get("lang") or "eng")
    if name == "camera_scan":
        return device.camera_scan(int(p.get("camera") or 0), p.get("lang") or "eng", p.get("image", ""))
    if name == "notify_ask":
        return device.notify_ask(p.get("question", ""), p.get("options"), int(p.get("timeout") or 300),
                                 p.get("title") or "TermuxGPT")
    if name == "recipe_share":
        return _recipe_share(p)
    if name == "cost_meter":
        return meter.reset() if b(p.get("reset")) else meter.report(p.get("task_id", ""))
    if name == "health_watch":
        action = p.get("action") or "report"
        return {"run": health.run, "report": health.report, "disable": health.disable}.get(
            action, lambda: health.enable(p.get("at") or "09:00"))()
    if name == "task_start":
        tool = p.get("tool", "")
        if tool not in SMART_TOOLS or tool.startswith("task_"):
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"{tool} is not a smart tool"}]}
        refusal = kernel.authorize_tool(tool, p.get("params") or {})
        if refusal:
            return {"ok": False, "errors": [{"code": "E_CAPABILITY", "msg": refusal}]}
        principal = kernel.current_principal()
        return tasks.start(tool, p.get("params") or {},
                           lambda t, prm: kernel.run_as(principal, run_smart_tool, t, prm))
    if name == "task_status":
        return tasks.status(p.get("task", ""))
    if name == "task_list":
        return tasks.list_tasks()
    return _dispatch_new(name, p)


def _as_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [v for v in re.split(r"[,\n]+", value) if v.strip()]
    return list(value) if isinstance(value, (list, tuple)) else None


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dispatch_new(name: str, p: dict) -> dict:
    b = base.boolish
    from .. import approval, auth, kernel, obs, policy
    if name == "learn_propose":
        return learn.learn_propose(p.get("steps"), str(p.get("task_id", "")), str(p.get("title", "")),
                                   str(p.get("request", "")))
    if name == "learn_save":
        return learn.learn_save(str(p.get("draft_id", "")), str(p.get("title", "")), _as_list(p.get("phrases")),
                                b(p.get("overwrite")))
    if name == "fix_learn":
        return learn.fix_learn(str(p.get("error", "")), str(p.get("code", "")), _as_list(p.get("fix")) or [],
                               str(p.get("verify", "")), b(p.get("worked"), True), str(p.get("desc", "")))
    if name == "fix_share":
        return learn.fix_share(str(p.get("action", "")), str(p.get("file", "")), b(p.get("confirmed")))
    if name == "plan_cache_get":
        return learn.plan_cache_get(str(p.get("request", "")))
    if name == "plan_cache_put":
        return learn.plan_cache_put(str(p.get("request", "")), p.get("steps") or [], verify=str(p.get("verify", "")))
    if name == "plan_replay":
        return learn.plan_replay(str(p.get("request", "")), p.get("steps"), b(p.get("confirmed")),
                                 str(p.get("task_id", "")))
    if name == "prompt_pack":
        return learn.prompt_pack(str(p.get("task_type", "")), str(p.get("text", "")))
    if name == "goal_run":
        return goal.goal_run(str(p.get("goal", "")), p.get("steps"), p.get("verify"), p.get("budget"),
                             b(p.get("confirmed")), b(p.get("rollback"), True), b(p.get("dry_run")))
    if name == "timeline":
        return timeline.timeline(int(_num(p.get("limit"), 20)))
    if name == "undo_task":
        return timeline.undo_task(str(p.get("task", "")), b(p.get("confirmed")))
    if name == "metrics":
        if b(p.get("reset")):
            obs.reset()
            return {"ok": True, "summary": "metrics reset"}
        return obs.metrics()
    if name == "policy_get":
        return policy.summary()
    if name in ADMIN_TOOLS and kernel.current_principal() is not None:
        return {"ok": False, "errors": [{"code": "E_CAPABILITY", "msg": "capability tokens cannot manage access"}]}
    if name == "policy_set":
        changes = p.get("policy")
        if not isinstance(changes, dict):
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "policy: {key: value}"}]}
        problems = policy.validate(changes)
        if problems:
            return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "; ".join(problems)}]}
        if not b(p.get("confirmed")):
            return {"ok": False, "needs_confirmation": True, "preview": changes,
                    "errors": [{"code": "E_CONFIRM", "msg": "resend with confirmed: true"}]}
        if _loosens(policy.get(), changes, b(p.get("replace"))) and not approval.spend("policy_set"):
            return {"ok": False, "needs_approval": True,
                    "errors": [{"code": "E_APPROVAL", "msg": "this loosens the policy: approve it on the device "
                                                            "first (approve action=policy_set), then resend"}]}
        return policy.set_policy(changes, b(p.get("replace")))
    if name == "cap_issue":
        if not b(p.get("confirmed")):
            return {"ok": False, "needs_confirmation": True,
                    "errors": [{"code": "E_CONFIRM", "msg": "this grants access to an external client; "
                                                           "resend with confirmed: true"}]}
        return auth.cap_issue(_as_list(p.get("tools")), _as_list(p.get("paths")), b(p.get("read_only")),
                              int(_num(p.get("ttl_min"), 60)), str(p.get("label", "")))
    if name == "cap_list":
        return auth.cap_list()
    if name == "cap_revoke":
        return auth.cap_revoke(str(p.get("id", "")))
    if name == "venv_ensure":
        return power.venv_ensure(str(p.get("path", "")), str(p.get("python") or "python"),
                                 str(p.get("requirements", "")), b(p.get("confirmed")))
    if name == "node_ensure":
        return power.node_ensure(str(p.get("path", "")), str(p.get("version", "")), b(p.get("confirmed")))
    if name == "proot_ensure":
        return power.proot_ensure(str(p.get("distro") or "debian"), b(p.get("confirmed")))
    if name == "proot_run":
        return power.proot_run(str(p.get("distro") or "debian"), str(p.get("cmd", "")),
                               int(_num(p.get("timeout"), 300)), b(p.get("confirmed")))
    if name == "cron_ensure":
        return power.cron_ensure(str(p.get("name", "")), str(p.get("spec", "")), str(p.get("cmd", "")),
                                 str(p.get("action") or "ensure"), b(p.get("confirmed")))
    if name == "tunnel":
        return power.tunnel(p.get("port") or 0, str(p.get("action") or "open"), int(_num(p.get("minutes"), 30)),
                            str(p.get("provider") or "auto"), str(p.get("id", "")), b(p.get("confirmed")))
    if name == "web_fetch":
        return power.web_fetch(str(p.get("url", "")), str(p.get("selector", "")), int(_num(p.get("max_chars"), 3000)))
    if name == "db_query":
        return power.db_query(str(p.get("path", "")), str(p.get("sql", "")), int(_num(p.get("limit"), 50)),
                              b(p.get("write")), b(p.get("confirmed")))
    if name == "log_watch":
        return power.log_watch(str(p.get("path", "")), str(p.get("pattern", "")), str(p.get("action") or "scan"),
                               str(p.get("notify", "")), str(p.get("cmd", "")), str(p.get("name", "")),
                               id=str(p.get("id", "")), confirmed=b(p.get("confirmed")))
    if name == "file_find":
        return power.file_find(str(p.get("query", "")), str(p.get("path") or "~"), str(p.get("kind") or "name"),
                               str(p.get("ext", "")), _num(p.get("newer_than_days")), _num(p.get("larger_than_mb")),
                               int(_num(p.get("limit"), 30)), b(p.get("refresh")))
    if name == "backup_incremental":
        return power.backup_incremental(str(p.get("action") or "backup"), str(p.get("src") or "~"),
                                        str(p.get("dest", "")), str(p.get("snapshot", "")), str(p.get("target", "")),
                                        str(p.get("password", "")), b(p.get("full")), b(p.get("confirmed")))
    if name == "clipboard_pipe":
        return power.clipboard_pipe(str(p.get("direction") or "get"), str(p.get("text", "")), str(p.get("file", "")),
                                    int(_num(p.get("max_chars"), 2000)))
    if name == "share_to":
        return power.share_to(str(p.get("path", "")), str(p.get("text", "")), str(p.get("url", "")),
                              str(p.get("action") or "send"), str(p.get("title", "")))
    if name == "media_convert":
        return power.media_convert(str(p.get("input", "")), str(p.get("output", "")), str(p.get("preset", "")),
                                   str(p.get("start", "")), str(p.get("duration", "")), int(_num(p.get("crf"), 28)),
                                   int(_num(p.get("width"), 480)), int(_num(p.get("fps"), 12)), b(p.get("overwrite")),
                                   str(p.get("action") or "convert"), b(p.get("background")))
    if name == "ssh_hosts":
        return power.ssh_hosts(str(p.get("action") or "list"), str(p.get("name", "")), str(p.get("host", "")),
                               str(p.get("user", "")), int(_num(p.get("port"), 22)), str(p.get("key", "")))
    if name == "ssh_run":
        return power.ssh_run(str(p.get("host", "")), str(p.get("cmd", "")), int(_num(p.get("timeout"), 60)),
                             b(p.get("confirmed")))
    return {"ok": False, "errors": [{"code": "E_UNKNOWN_TOOL", "msg": name}]}


def _loosens(current: dict, changes: dict, replace: bool) -> bool:
    from .. import policy
    after = dict(policy.DEFAULT) if replace else dict(current)
    after.update(changes)
    for key in ("deny_paths", "deny_tools"):
        if set(current.get(key) or []) - set(after.get(key) or []):
            return True
    if current.get("allow_paths") and (not after.get("allow_paths")
                                       or set(after["allow_paths"]) - set(current["allow_paths"])):
        return True
    if current.get("network") is False and after.get("network") is not False:
        return True
    if current.get("pip") == "venv_only" and after.get("pip") != "venv_only":
        return True
    if current.get("sandbox_unknown_scripts") and not after.get("sandbox_unknown_scripts"):
        return True
    return False


def run_smart_tool(name: str, params: dict) -> dict:
    p = params if isinstance(params, dict) else {}
    try:
        body = _dispatch(name, p)
    except Exception as error:
        body = {"ok": False, "errors": [{"code": "E_INTERNAL", "msg": f"{type(error).__name__}: {error}"}]}
    if not isinstance(body, dict):
        body = {"ok": True, "value": body}
    raw = body.pop("_raw", 0)
    out = base.result(body)
    try:
        meter.record(name, raw or len(out["text"]), len(out["text"]), str(p.get("task_id") or p.get("_task") or ""))
    except Exception:
        pass
    try:
        reactor.ensure_running()
    except Exception:
        pass
    return out


def digest_text(cmd: str, text: str, exit_code: int = 0) -> dict:
    return digest.make(cmd, exit_code, text)
