import difflib
import json
import os
import re
import shlex
import shutil
import time

from .. import safety
from . import base, cache, digest

PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9.+_-]*$", re.IGNORECASE)


def _pkg_version(name: str) -> str:
    res = base.sh(f"dpkg-query -W -f='${{Version}}' {shlex.quote(name)} 2>/dev/null",
                  timeout=15, check_risk=False)
    return res.out.strip() if res.ok else ""


def _pip_version(name: str) -> str:
    res = base.sh(f"pip show {shlex.quote(name)} 2>/dev/null | sed -n 's/^Version: //p'",
                  timeout=30, check_risk=False)
    return res.out.strip()


def pkg_ensure(names, manager: str = "pkg", task_id: str = "") -> dict:
    if isinstance(names, str):
        names = [n for n in re.split(r"[\s,]+", names) if n]
    names = [n for n in (names or []) if PKG_NAME.match(str(n))]
    if not names:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "names: list of package names"}]}
    manager = (manager or "pkg").lower()
    get = _pip_version if manager == "pip" else _pkg_version
    have = {n: get(n) for n in names}
    missing = [n for n, v in have.items() if not v]
    body = {"ok": True, "already": [n for n in names if have[n]]}
    if missing:
        quoted = " ".join(shlex.quote(n) for n in missing)
        cmd = (f"pip install {quoted} --break-system-packages" if manager == "pip"
               else f"pkg install -y {quoted}")
        res = base.sh(cmd, timeout=900, confirmed=True)
        body["install"] = {k: v for k, v in digest.from_shell(res).items()
                           if k in ("ok", "exit", "summary", "errors", "excerpt", "out_ref")}
        cache.invalidate("pkg", "graph", "context")
        try:
            from . import graph
            graph.on_package_change(missing)
        except Exception:
            pass
    final = {n: get(n) for n in names}
    body["versions"] = {n: v for n, v in final.items() if v}
    body["failed"] = [n for n, v in final.items() if not v]
    body["ok"] = not body["failed"]
    body["summary"] = (", ".join(f"{n} {v}" for n, v in body["versions"].items()) or "nothing installed") + \
        (f"; failed: {', '.join(body['failed'])}" if body["failed"] else "")
    if body["failed"]:
        from . import fixgraph
        errs = body.get("install", {}).get("errors") or [{"code": "E_PKG_NOT_FOUND", "subject": body["failed"][0]}]
        body["errors"] = errs
        body["fixes"] = fixgraph.lookup(code=errs[0]["code"], subject=errs[0].get("subject") or body["failed"][0],
                                        cmd="")["fixes"][:2]
    return body


def _abs(path: str) -> str:
    path = os.path.expanduser(str(path or ""))
    if not os.path.isabs(path):
        path = os.path.join(base.home(), path)
    return os.path.normpath(path)


def file_edit(path: str, find: str = "", replace: str = "", content=None,
              count: int = 0, regex: bool = False, append: str = "",
              task_id: str = "", dry_run: bool = False) -> dict:
    target = _abs(path)
    if not path:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "path required"}]}
    from ..utils import is_sensitive_path
    if is_sensitive_path(target):
        return {"ok": False, "errors": [{"code": "E_SENSITIVE", "msg": f"{target} is protected"}]}
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            before = handle.read()
        exists = True
    except FileNotFoundError:
        before, exists = "", False
    except OSError as error:
        return {"ok": False, "errors": [{"code": "E_IO", "msg": str(error)}]}

    if content is not None:
        after = str(content)
    elif find:
        if regex:
            try:
                rx = re.compile(find, re.MULTILINE)
            except re.error as error:
                return {"ok": False, "errors": [{"code": "E_ARGS", "msg": f"bad regex: {error}"}]}
            hits = len(rx.findall(before))
            after = rx.sub(replace, before, count=int(count or 0))
        else:
            hits = before.count(find)
            after = before.replace(find, replace, int(count) if count else -1)
        if hits == 0:
            return {"ok": False, "errors": [{"code": "E_NO_MATCH", "msg": "find text not in file"}],
                    "hint": "re-read the exact lines with read/out_read and retry"}
    elif append:
        after = before + ("" if before.endswith("\n") or not before else "\n") + append
    else:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "give find/replace, content or append"}]}

    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    body = {"path": target, "created": not exists, "added": added, "removed": removed,
            "diff": "\n".join(diff[:40])}
    if dry_run or before == after:
        body.update(ok=True, dry_run=bool(dry_run), unchanged=before == after,
                    summary=f"{'would change' if dry_run else 'no change to'} {os.path.basename(target)}")
        return body
    snap = safety.snapshot_before_write(target, tool="file_edit", task_id=task_id)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(after)
        with open(target, encoding="utf-8", errors="replace") as handle:
            verified = handle.read() == after
    except OSError as error:
        return {"ok": False, "errors": [{"code": "E_IO", "msg": str(error)}]}
    body.update(ok=verified, verified=verified, snapshot=snap or "",
                summary=f"edited {os.path.basename(target)}: +{added} -{removed}")
    if not verified:
        body["errors"] = [{"code": "E_VERIFY", "msg": "file content differs after write"}]
    return body


def _svc_dir() -> str:
    return base.ensure_dir(base.root("services"))


def _svc_file(name: str) -> str:
    return os.path.join(_svc_dir(), re.sub(r"[^\w.-]", "_", name) + ".json")


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def port_listening(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def service_table() -> list:
    rows = []
    for name in sorted(os.listdir(_svc_dir())):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_svc_dir(), name), encoding="utf-8") as handle:
                spec = json.load(handle)
        except (OSError, ValueError):
            continue
        pid = spec.get("pid") if _alive(spec.get("pid")) else None
        rows.append((spec.get("name"), spec.get("cmd"), spec.get("port"), pid, spec.get("updated", "")))
    return rows


def service_ensure(name: str, cmd: str = "", port: int = 0, cwd: str = "",
                   restart: bool = False, wait: int = 15) -> dict:
    if not name:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "name required"}]}
    path = _svc_file(name)
    spec = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                spec = json.load(handle)
        except (OSError, ValueError):
            spec = {}
    cmd = cmd or spec.get("cmd", "")
    port = int(port or spec.get("port") or 0)
    cwd = _abs(cwd) if cwd else spec.get("cwd", base.home())
    if not cmd:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "cmd required the first time"}]}
    pid = spec.get("pid")
    up = _alive(pid) and (not port or port_listening(port))
    if up and not restart:
        return {"ok": True, "up": True, "pid": pid, "port": port or None,
                **({"url": f"http://localhost:{port}"} if port else {}),
                "summary": f"{name} already running (pid {pid})"}
    if _alive(pid):
        service_stop(name)
    from ..security import get_risk_assessment
    risk = get_risk_assessment(cmd)
    if risk["blocked"]:
        return {"ok": False, "errors": [{"code": "E_BLOCKED", "msg": risk["message"]}]}
    log = os.path.join(_svc_dir(), re.sub(r"[^\w.-]", "_", name) + ".log")
    import subprocess
    with open(log, "ab") as out:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    spec = {"name": name, "cmd": cmd, "port": port or None, "cwd": cwd, "pid": proc.pid,
            "log": log, "updated": base.now_iso()}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(spec, handle)
    deadline = time.time() + (max(int(wait), 1) if port else 1.5)
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if port and port_listening(port):
            break
        time.sleep(0.3)
    alive = proc.poll() is None
    healthy = alive and (not port or port_listening(port))
    tail = ""
    try:
        with open(log, encoding="utf-8", errors="replace") as handle:
            tail = "\n".join(handle.read().splitlines()[-8:])
    except OSError:
        pass
    body = {"ok": healthy, "up": healthy, "pid": proc.pid if alive else None, "port": port or None,
            "log": log, "summary": f"{name} {'up' if healthy else 'failed to start'}"
                                   + (f" on :{port}" if port and healthy else "")}
    if port and healthy:
        body["url"] = f"http://localhost:{port}"
    if not healthy:
        body["log_tail"] = tail
        body["errors"] = digest.classify(tail, 1)
    cache.invalidate("graph")
    return body


def service_stop(name: str) -> dict:
    path = _svc_file(name)
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError):
        return {"ok": False, "errors": [{"code": "E_NO_SERVICE", "msg": f"no service {name}"}]}
    pid = spec.get("pid")
    import signal
    stopped = False
    if _alive(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(int(pid), sig) if hasattr(os, "killpg") else os.kill(int(pid), sig)
            except OSError:
                pass
            for _ in range(10):
                if not _alive(pid):
                    break
                time.sleep(0.2)
            if not _alive(pid):
                stopped = True
                break
    spec["pid"] = None
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(spec, handle)
    return {"ok": True, "stopped": stopped, "summary": f"{name} stopped"}


def _port_pids(port: int) -> list:
    probes = (
        (f"lsof -ti tcp:{port} -sTCP:LISTEN 2>/dev/null", r"^(\d+)$"),
        (f"fuser {port}/tcp 2>/dev/null", r"(?:^|\s)(\d+)(?=\s|$)"),
        (f"ss -ltnpH 'sport = :{port}' 2>/dev/null", r"pid=(\d+)"),
        (f"netstat -ltnp 2>/dev/null | grep -E ':{port}\\s'", r"\s(\d+)/\S+\s*$"),
    )
    for cmd, rx in probes:
        res = base.sh(cmd, timeout=8, check_risk=False)
        pids = {int(p) for p in re.findall(rx, res.out, re.MULTILINE)}
        pids.discard(os.getpid())
        pids = {p for p in pids if os.path.exists(f"/proc/{p}")} or pids
        if pids:
            return sorted(pids)
    return []


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode(errors="replace").strip()[:120]
    except OSError:
        return ""


def port_free(port, confirmed: bool = False) -> dict:
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "port must be a number"}]}
    if not port_listening(port):
        return {"ok": True, "free": True, "summary": f"port {port} is already free"}
    pids = _port_pids(port)
    who = [{"pid": p, "cmd": _comm(p)} for p in pids]
    if not confirmed:
        return {"ok": False, "free": False, "holders": who, "needs_confirmation": True,
                "errors": [{"code": "E_CONFIRM", "msg": "resend with confirmed: true to stop them"}],
                "summary": f"port {port} held by " + (", ".join(f"{w['pid']} {w['cmd'][:40]}" for w in who) or "an unknown process")}
    import signal
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(15):
        if not port_listening(port):
            break
        time.sleep(0.2)
    if port_listening(port):
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.5)
    free = not port_listening(port)
    return {"ok": free, "free": free, "stopped": who,
            "summary": f"port {port} {'freed' if free else 'still in use'}",
            **({} if free else {"errors": [{"code": "E_PORT_IN_USE", "msg": "could not free it"}]})}


def project_profile(path: str = "") -> dict:
    root = _abs(path or os.getcwd())
    if not os.path.isdir(root):
        return {"ok": False, "errors": [{"code": "E_NO_FILE", "msg": f"{root} is not a folder"}]}
    cached = cache.get("project", root)
    if cached:
        return cached
    files = set(os.listdir(root))
    prof = {"ok": True, "path": root, "lang": "", "install": "", "run": "", "test": "",
            "git": ".git" in files, "manifests": []}

    def read(name):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as h:
                return h.read()
        except OSError:
            return ""

    if "package.json" in files:
        prof["manifests"].append("package.json")
        prof["lang"] = "node"
        try:
            pj = json.loads(read("package.json") or "{}")
        except ValueError:
            pj = {}
        scripts = pj.get("scripts") or {}
        prof["install"] = "npm install"
        prof["run"] = "npm start" if "start" in scripts else (
            "npm run dev" if "dev" in scripts else f"node {pj.get('main', 'index.js')}")
        prof["test"] = "npm test" if "test" in scripts else ""
        prof["name"] = pj.get("name", "")
    elif files & {"requirements.txt", "pyproject.toml", "setup.py"} or any(f.endswith(".py") for f in files):
        prof["lang"] = "python"
        if "requirements.txt" in files:
            prof["manifests"].append("requirements.txt")
            prof["install"] = "pip install -r requirements.txt --break-system-packages"
        elif "pyproject.toml" in files or "setup.py" in files:
            prof["manifests"].append("pyproject.toml" if "pyproject.toml" in files else "setup.py")
            prof["install"] = "pip install . --break-system-packages"
        entry = next((f for f in ("main.py", "app.py", "bot.py", "run.py", "manage.py", "server.py", "__main__.py")
                      if f in files), "")
        if not entry:
            pys = sorted(f for f in files if f.endswith(".py"))
            entry = pys[0] if len(pys) == 1 else ""
        prof["run"] = f"python {entry}" + (" runserver" if entry == "manage.py" else "") if entry else ""
        prof["test"] = "python -m pytest -q" if ("tests" in files or "test" in files) else ""
    elif "Cargo.toml" in files:
        prof.update(lang="rust", install="cargo build --release", run="cargo run --release", test="cargo test")
        prof["manifests"].append("Cargo.toml")
    elif "go.mod" in files:
        prof.update(lang="go", install="go mod download", run="go run .", test="go test ./...")
        prof["manifests"].append("go.mod")
    elif "Makefile" in files:
        prof.update(lang="make", install="make", run="make run", test="make test")
        prof["manifests"].append("Makefile")
    elif any(f.endswith(".sh") for f in files):
        sh_files = sorted(f for f in files if f.endswith(".sh"))
        prof.update(lang="shell", run=f"bash {sh_files[0]}")
    readme = next((f for f in files if f.lower().startswith("readme")), "")
    if readme:
        text = read(readme)
        blocks = re.findall(r"```(?:bash|sh|shell)?\n(.+?)```", text, re.S)
        cmds = [l.strip().lstrip("$ ") for b in blocks for l in b.splitlines() if l.strip()][:8]
        if cmds:
            prof["readme_cmds"] = cmds
    prof["summary"] = f"{prof['lang'] or 'unknown'} project" + (f", run: {prof['run']}" if prof["run"] else "")
    return cache.put("project", prof, root)


def project_run(path: str = "", install: bool = True, test: bool = False,
                timeout: int = 300, confirmed: bool = False) -> dict:
    prof = project_profile(path)
    if not prof.get("ok"):
        return prof
    steps = []
    order = []
    if install and prof.get("install"):
        order.append(("install", prof["install"]))
    if test and prof.get("test"):
        order.append(("test", prof["test"]))
    elif prof.get("run"):
        order.append(("run", prof["run"]))
    if not order:
        return {"ok": False, "profile": prof,
                "errors": [{"code": "E_NO_ENTRY", "msg": "could not find how to run this project"}]}
    ok = True
    for label, cmd in order:
        res = base.sh(cmd, timeout=timeout, cwd=prof["path"], confirmed=confirmed or label != "run")
        d = digest.from_shell(res)
        steps.append({"step": label, "cmd": cmd, **{k: v for k, v in d.items()
                                                    if k in ("ok", "exit", "summary", "errors", "excerpt",
                                                             "out", "out_ref", "facts")}})
        if not d["ok"] and not (label == "run" and res.timed_out):
            ok = False
            from . import fixgraph
            if d.get("errors"):
                steps[-1]["fixes"] = fixgraph.lookup(code=d["errors"][0]["code"],
                                                     subject=d["errors"][0].get("subject", ""),
                                                     cmd=cmd)["fixes"][:2]
            break
    return {"ok": ok, "profile": {k: prof[k] for k in ("lang", "run", "install", "test")},
            "steps": steps, "summary": " → ".join(f"{s['step']} {'ok' if s['ok'] else 'failed'}" for s in steps)}


def _du(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _categories():
    home = base.home()
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return {
        "apt_cache": [os.path.join(prefix, "var", "cache", "apt", "archives")],
        "pip_cache": [os.path.join(home, ".cache", "pip")],
        "npm_cache": [os.path.join(home, ".npm", "_cacache")],
        "tmp": [os.path.join(prefix, "tmp")],
        "pycache": "__pycache__",
        "old_logs": "*.log",
        "trash": [safety.safety_root("trash")],
    }


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def storage_clean(categories=None, dry_run: bool = True, confirmed: bool = False,
                  task_id: str = "") -> dict:
    cats = _categories()
    wanted = categories or [c for c in cats if c != "trash"]
    if isinstance(wanted, str):
        wanted = [w for w in re.split(r"[\s,]+", wanted) if w]
    home = base.home()
    plan = {}
    for cat in wanted:
        spec = cats.get(cat)
        if spec is None:
            continue
        targets = []
        if cat == "pycache":
            for dirpath, dirnames, _ in os.walk(home):
                if "__pycache__" in dirnames:
                    targets.append(os.path.join(dirpath, "__pycache__"))
                dirnames[:] = [d for d in dirnames if d not in ("termuxGPT", "storage", ".git", "node_modules")]
        elif cat == "old_logs":
            cutoff = time.time() - 7 * 86400
            for dirpath, dirnames, filenames in os.walk(home):
                dirnames[:] = [d for d in dirnames if d not in ("termuxGPT", "storage", ".git")]
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    try:
                        if f.endswith(".log") and os.path.getmtime(full) < cutoff \
                                and os.path.getsize(full) > 256 * 1024:
                            targets.append(full)
                    except OSError:
                        pass
        else:
            targets = [p for p in spec if os.path.exists(p)]
        size = sum(_du(t) if os.path.isdir(t) else os.path.getsize(t) for t in targets if os.path.exists(t))
        plan[cat] = {"bytes": size, "size": _human(size), "items": len(targets), "_targets": targets}
    total = sum(p["bytes"] for p in plan.values())
    public = {k: {x: y for x, y in v.items() if not x.startswith("_")} for k, v in plan.items()}
    if dry_run or not confirmed:
        return {"ok": True, "dry_run": True, "reclaimable": _human(total), "categories": public,
                "summary": f"can free {_human(total)}; resend with dry_run: false, confirmed: true",
                **({} if dry_run else {"needs_confirmation": True})}
    before = shutil.disk_usage(home).free
    freed_items = 0
    for cat, info in plan.items():
        for target in info["_targets"]:
            if cat == "trash":
                shutil.rmtree(target, ignore_errors=True)
                os.makedirs(target, exist_ok=True)
                freed_items += 1
            elif cat in ("apt_cache", "pip_cache", "npm_cache", "tmp") and os.path.isdir(target):
                for name in os.listdir(target):
                    full = os.path.join(target, name)
                    if name in ("lock", "partial") or name.startswith(("tmux-", ".X", "termux-mcp")):
                        continue
                    try:
                        if cat == "tmp" and time.time() - os.path.getmtime(full) < 3600:
                            continue
                    except OSError:
                        continue
                    try:
                        shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.remove(full)
                        freed_items += 1
                    except OSError:
                        pass
            else:
                if safety.trash_path(target, tool="storage_clean", task_id=task_id):
                    freed_items += 1
    after = shutil.disk_usage(home).free
    return {"ok": True, "freed": _human(max(after - before, 0)), "estimated": _human(total),
            "items": freed_items, "categories": public,
            "summary": f"freed {_human(max(after - before, 0))} (logs/pycache are in trash and can be undone)"}


def git_sync(path: str = "", pull: bool = True) -> dict:
    repo = _abs(path or os.getcwd())
    q = shlex.quote(repo)
    top = base.sh(f"git -C {q} rev-parse --show-toplevel", timeout=10, check_risk=False)
    if not top.ok:
        return {"ok": False, "errors": digest.classify(top.text, 1) or
                [{"code": "E_GIT_NOT_REPO", "msg": "not a git repo"}]}
    fetch = base.sh(f"git -C {q} fetch --quiet", timeout=120, check_risk=False)
    status = base.sh(f"git -C {q} status -sb --porcelain", timeout=15, check_risk=False)
    lines = status.out.splitlines()
    head = lines[0] if lines else ""
    ahead = int((re.search(r"ahead (\d+)", head) or [0, 0])[1])
    behind = int((re.search(r"behind (\d+)", head) or [0, 0])[1])
    dirty = [l[3:] for l in lines[1:]]
    body = {"path": top.out.strip(), "branch": head[3:].split("...")[0] if head.startswith("## ") else "",
            "ahead": ahead, "behind": behind, "dirty": dirty[:20], "fetched": fetch.ok}
    if not fetch.ok:
        body["fetch_error"] = digest.classify(fetch.text, 1)
    pulled = None
    if pull and behind and not dirty:
        res = base.sh(f"git -C {q} pull --ff-only", timeout=180, check_risk=False)
        pulled = res.ok
        if not res.ok:
            body["errors"] = digest.classify(res.text, 1)
    body["pulled"] = pulled
    body["ok"] = not body.get("errors")
    parts = [body["branch"] or "repo", f"ahead {ahead}", f"behind {behind}"]
    if dirty:
        parts.append(f"{len(dirty)} local changes")
    if pulled:
        parts.append("pulled")
    elif behind and dirty:
        parts.append("not pulled (local changes)")
    body["summary"] = ", ".join(parts)
    return body
