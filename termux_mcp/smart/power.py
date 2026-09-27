import csv
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser

from .. import policy, safety
from . import base, digest

q = shlex.quote
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", "termuxGPT", ".npm", ".venv", "venv",
             ".gradle", ".pub-cache", ".cargo", ".rustup", "storage", ".termux-mcp", ".local"}
NAME_OK = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")


def _err(code, msg, **extra):
    body = {"ok": False, "errors": [{"code": code, "msg": msg}]}
    body.update(extra)
    return body


def _path(p, default=""):
    p = str(p or default or "").strip()
    if not p:
        return ""
    if p.startswith("~"):
        p = os.path.join(base.home(), p[1:].lstrip("/"))
    elif p.startswith("$HOME"):
        p = base.home() + p[5:]
    return os.path.normpath(os.path.abspath(p))


def _policy_path(p, write=False):
    return policy.check_path(p, write=write)


def _sha_file(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _read_small(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(4096).strip()
    except OSError:
        return ""


def _run(cmd, timeout=None, cwd=None, confirmed=False, stdin=None):
    res = base.sh(cmd, timeout=timeout, cwd=cwd, confirmed=confirmed, stdin=stdin)
    body = digest.from_shell(res)
    body["cmd"] = cmd
    return res, body


def _brief(body):
    return {k: body[k] for k in ("ok", "exit", "summary", "errors", "out_ref", "excerpt") if k in body}


def venv_ensure(path: str, python: str = "python", requirements: str = "", confirmed: bool = False) -> dict:
    root = _path(path)
    if not root:
        return _err("E_ARGS", "path: the project folder")
    reason = _policy_path(root, write=True)
    if reason:
        return _err("E_POLICY", reason)
    if not re.match(r"^[\w.+/-]+$", python or "python"):
        return _err("E_ARGS", "python: an interpreter name like python or python3.11")
    os.makedirs(root, exist_ok=True)
    venv = os.path.join(root, ".venv")
    vpy = os.path.join(venv, "bin", "python")
    steps, created = [], False
    if not os.path.exists(vpy):
        res, body = _run(f"{q(python)} -m venv {q(venv)}", timeout=300, cwd=root)
        steps.append(_brief(body))
        if not res.ok:
            return {"ok": False, "steps": steps, "errors": body.get("errors") or
                    [{"code": "E_VENV", "msg": "could not create the venv"}]}
        created = True
    req = _path(requirements) if requirements else os.path.join(root, "requirements.txt")
    installed = cached = False
    if os.path.isfile(req):
        want = _sha_file(req)
        stamp = os.path.join(venv, ".tmcp_req_hash")
        have = _read_small(stamp)
        if want == have:
            cached = True
        else:
            res, body = _run(f"{q(vpy)} -m pip install --disable-pip-version-check -q -r {q(req)}",
                             timeout=1200, cwd=root, confirmed=True)
            steps.append(_brief(body))
            if not res.ok:
                return {"ok": False, "steps": steps, "venv": venv,
                        "errors": body.get("errors") or [{"code": "E_PIP", "msg": "pip install failed"}],
                        **({"fixes": body["fixes"]} if body.get("fixes") else {})}
            with open(stamp, "w") as handle:
                handle.write(want)
            installed = True
    ver = base.sh(f"{q(vpy)} --version", timeout=20, check_risk=False).text.strip()
    count = base.sh(f"{q(vpy)} -m pip list --format=freeze 2>/dev/null | wc -l", timeout=60,
                    check_risk=False).out.strip()
    return {"ok": True, "venv": venv, "python": vpy, "pip": os.path.join(venv, "bin", "pip"),
            "version": ver, "packages": int(count) if count.isdigit() else None, "created": created,
            "requirements": req if os.path.isfile(req) else None, "installed": installed, "cached": cached,
            "activate": f"source {venv}/bin/activate", "steps": steps or None,
            "summary": f"venv ready ({ver})" + (", requirements installed" if installed else
                                                 ", requirements unchanged" if cached else "")}


def _node_major(text: str):
    m = re.search(r"v?(\d+)\.", text or "")
    return int(m.group(1)) if m else None


def node_ensure(path: str = "", version: str = "", confirmed: bool = False) -> dict:
    from .intent_tools import pkg_ensure
    steps = []
    have = base.sh("node --version", timeout=15, check_risk=False)
    if not have.ok:
        pkg = "nodejs-lts" if str(version).lower() in ("lts",) else "nodejs"
        r = pkg_ensure([pkg])
        steps.append({"pkg_ensure": pkg, "ok": r.get("ok"), "summary": r.get("summary")})
        if not r.get("ok"):
            return {"ok": False, "steps": steps, "errors": r.get("errors") or [{"code": "E_PKG", "msg": pkg}]}
        have = base.sh("node --version", timeout=15, check_risk=False)
    current = have.out.strip()
    want = str(version or "").strip().lower().lstrip("v")
    mismatch = None
    if want and want != "lts":
        wm = _node_major(want + ".")
        cm = _node_major(current)
        if wm and cm and wm != cm:
            mismatch = {"want": wm, "have": cm}
    if mismatch:
        lts = base.sh("apt-cache policy nodejs-lts 2>/dev/null | sed -n 's/ *Candidate: //p'", timeout=20,
                      check_risk=False).out.strip()
        if lts and _node_major(lts) == mismatch["want"]:
            if not confirmed:
                return _err("E_CONFIRM", f"node {mismatch['have']} is installed; nodejs-lts {lts} matches "
                            f"v{mismatch['want']} — resend with confirmed: true to switch",
                            needs_confirmation=True, have=current, candidate=lts)
            res, body = _run("pkg uninstall -y nodejs && pkg install -y nodejs-lts", timeout=900, confirmed=True)
            steps.append(_brief(body))
            if not res.ok:
                return {"ok": False, "steps": steps, "errors": body.get("errors")}
            current = base.sh("node --version", timeout=15, check_risk=False).out.strip()
        else:
            return _err("E_VERSION", f"node v{mismatch['want']} is not packaged for Termux (have {current}, "
                        f"nodejs-lts is {lts or 'unavailable'})", have=current)
    result = {"ok": True, "node": current,
              "npm": base.sh("npm --version", timeout=20, check_risk=False).out.strip() or None}
    if path:
        root = _path(path)
        pj = os.path.join(root, "package.json")
        if not os.path.isfile(pj):
            return dict(result, ok=False, errors=[{"code": "E_NO_FILE", "msg": f"no package.json in {root}"}])
        reason = _policy_path(root, write=True)
        if reason:
            return _err("E_POLICY", reason)
        locks = [f for f in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml") if os.path.isfile(os.path.join(root, f))]
        want_hash = hashlib.sha1("".join(_sha_file(os.path.join(root, f)) for f in ["package.json"] + locks)
                                 .encode()).hexdigest()
        stamp = os.path.join(root, "node_modules", ".tmcp_hash")
        have_hash = _read_small(stamp)
        if want_hash == have_hash:
            result.update(deps="unchanged", cached=True)
        else:
            cmd = "npm ci --no-audit --no-fund" if "package-lock.json" in locks else "npm install --no-audit --no-fund"
            res, body = _run(cmd, timeout=1500, cwd=root, confirmed=True)
            steps.append(_brief(body))
            if not res.ok:
                return dict(result, ok=False, steps=steps, errors=body.get("errors") or
                            [{"code": "E_NPM", "msg": "npm install failed"}])
            os.makedirs(os.path.dirname(stamp), exist_ok=True)
            with open(stamp, "w") as handle:
                handle.write(want_hash)
            result.update(deps="installed", cached=False)
    result["steps"] = steps or None
    result["summary"] = f"node {current}" + (f", deps {result['deps']}" if result.get("deps") else "")
    return result


DISTRO_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")


def _rootfs(distro: str) -> str:
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return os.path.join(prefix, "var", "lib", "proot-distro", "installed-rootfs", distro)


def proot_ensure(distro: str = "debian", confirmed: bool = False) -> dict:
    from .intent_tools import pkg_ensure
    distro = str(distro or "debian").lower()
    if not DISTRO_OK.match(distro):
        return _err("E_ARGS", "distro: e.g. debian, ubuntu, archlinux, alpine")
    if not base.which("proot-distro"):
        r = pkg_ensure(["proot-distro"])
        if not r.get("ok"):
            return {"ok": False, "errors": r.get("errors") or [{"code": "E_PKG", "msg": "proot-distro"}]}
    if os.path.isdir(os.path.join(_rootfs(distro), "etc")):
        return {"ok": True, "distro": distro, "installed": True, "new": False,
                "run": f"proot_run distro={distro} cmd=...", "summary": f"{distro} is ready"}
    if not confirmed:
        return _err("E_CONFIRM", f"installing {distro} downloads a few hundred MB; resend with confirmed: true",
                    needs_confirmation=True)
    res, body = _run(f"proot-distro install {q(distro)}", timeout=2400, confirmed=True)
    if not res.ok and "already installed" not in res.text.lower():
        return {"ok": False, "errors": body.get("errors") or [{"code": "E_PROOT", "msg": "install failed"}],
                "out_ref": body.get("out_ref")}
    return {"ok": True, "distro": distro, "installed": True, "new": True, "summary": f"{distro} installed"}


def proot_run(distro: str, cmd: str, timeout: int = 300, confirmed: bool = False) -> dict:
    distro = str(distro or "debian").lower()
    if not DISTRO_OK.match(distro) or not str(cmd or "").strip():
        return _err("E_ARGS", "distro and cmd")
    if not os.path.isdir(os.path.join(_rootfs(distro), "etc")):
        return _err("E_NOT_INSTALLED", f"{distro} is not installed: proot_ensure first")
    full = f"proot-distro login {q(distro)} --shared-tmp -- bash -lc {q(cmd)}"
    res, body = _run(full, timeout=int(timeout or 300), confirmed=confirmed)
    body["distro"] = distro
    body["cmd"] = cmd
    return body


CRON_FIELD = r"(\*|\d+|\d+-\d+|\*/\d+|\d+(,\d+)+|\d+-\d+/\d+)"
CRON_SPEC = re.compile(rf"^{CRON_FIELD}(\s+{CRON_FIELD}){{4}}$|^@(reboot|hourly|daily|weekly|monthly|yearly)$")


def _crontab() -> str:
    res = base.sh("crontab -l 2>/dev/null", timeout=15, check_risk=False)
    return res.out if res.exit == 0 else ""


def cron_ensure(name: str = "", spec: str = "", cmd: str = "", action: str = "ensure",
                confirmed: bool = False) -> dict:
    from .intent_tools import pkg_ensure
    from .. import kernel
    action = action or "ensure"
    if not base.which("crontab"):
        if action == "list":
            return {"ok": True, "jobs": [], "summary": "cron is not installed"}
        r = pkg_ensure(["cronie"])
        if not r.get("ok"):
            return {"ok": False, "errors": r.get("errors") or [{"code": "E_PKG", "msg": "cronie"}]}
    current = _crontab()
    lines = [ln for ln in current.splitlines()]
    if action == "list":
        jobs = []
        for ln in lines:
            m = re.search(r"#\s*tmcp:([\w.-]+)\s*$", ln)
            if ln.strip() and not ln.lstrip().startswith("#"):
                jobs.append({"name": m.group(1) if m else None, "line": ln[:200]})
        return {"ok": True, "jobs": jobs, "summary": f"{len(jobs)} cron job(s)"}
    if not NAME_OK.match(str(name or "")):
        return _err("E_ARGS", "name: letters, digits, . _ -")
    marker = f"# tmcp:{name}"
    keep = [ln for ln in lines if not ln.rstrip().endswith(marker)]
    log = os.path.join(base.ensure_dir(base.root("cron")), f"{name}.log")
    if action == "remove":
        if len(keep) == len(lines):
            return {"ok": True, "removed": 0, "summary": f"no job named {name}"}
        new_tab = "\n".join(keep).strip() + "\n"
    elif action == "ensure":
        spec = " ".join(str(spec or "").split())
        if not CRON_SPEC.match(spec):
            return _err("E_ARGS", "spec: 5 cron fields like '*/15 * * * *' or @daily")
        if not str(cmd or "").strip() or "\n" in cmd:
            return _err("E_ARGS", "cmd: one line")
        decision = kernel.gate(cmd, confirmed=confirmed, tool="cron_ensure")
        if decision["status"] == "confirm":
            return _err("E_CONFIRM", decision["message"] + " — resend with confirmed: true", needs_confirmation=True)
        if not decision["allow"]:
            return _err(decision["code"], decision["message"])
        line = f"{spec} {cmd} >> {log} 2>&1 {marker}"
        if line in lines:
            _ensure_crond()
            return {"ok": True, "name": name, "line": line, "changed": False, "log": log,
                    "running": _crond_running(), "summary": f"{name} already scheduled ({spec})"}
        new_tab = "\n".join(keep + [line]).strip() + "\n"
    else:
        return _err("E_ARGS", "action: ensure|remove|list")
    backup = os.path.join(base.ensure_dir(base.root("cron")), f"crontab-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    with open(backup, "w") as handle:
        handle.write(current)
    res = base.sh("crontab -", timeout=15, stdin=new_tab, confirmed=True)
    if not res.ok:
        return _err("E_CRON", res.text.strip()[:200] or "crontab write failed", backup=backup)
    after = _crontab()
    if action == "remove":
        ok = marker not in after
        return {"ok": ok, "removed": len(lines) - len(keep), "backup": backup,
                "summary": f"removed {name}" if ok else "remove did not take"}
    ok = line in after.splitlines()
    running = _ensure_crond()
    return {"ok": ok, "name": name, "line": line, "changed": True, "log": log, "backup": backup,
            "running": running, "verified": ok,
            "summary": (f"scheduled {name} ({spec}); log {log}" if ok else "crontab did not keep the line")
                       + ("" if running else " — crond is not running (pkg install termux-services; sv-enable crond)")}


def _crond_running() -> bool:
    return base.sh("pgrep -x crond >/dev/null", timeout=10, check_risk=False).ok


def _ensure_crond() -> bool:
    if _crond_running():
        return True
    base.sh("crond 2>/dev/null", timeout=10, check_risk=False)
    time.sleep(0.3)
    return _crond_running()


_URL_CF = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_URL_SSH = re.compile(r"https://[a-z0-9-]+\.(?:lhr\.life|localhost\.run|lhr\.rocks)")


def _tunnels():
    st = base.load_state("tunnels", {})
    alive = {}
    for tid, t in st.items():
        try:
            os.kill(int(t["pid"]), 0)
            if t.get("expires", 0) > time.time():
                alive[tid] = t
        except (OSError, ValueError, KeyError):
            continue
    if alive != st:
        base.save_state("tunnels", alive)
    return alive


def tunnel(port: int = 0, action: str = "open", minutes: int = 30, provider: str = "auto",
           id: str = "", confirmed: bool = False) -> dict:
    from .. import kernel
    from .intent_tools import port_listening
    action = action or "open"
    if action == "list":
        rows = [{"id": k, **{x: v[x] for x in ("port", "url", "provider")},
                 "minutes_left": max(0, int((v["expires"] - time.time()) / 60))} for k, v in _tunnels().items()]
        return {"ok": True, "tunnels": rows, "summary": f"{len(rows)} open tunnel(s)"}
    if action == "stop":
        st = _tunnels()
        targets = [id] if id else list(st)
        stopped = 0
        for tid in targets:
            t = st.pop(tid, None)
            if t:
                try:
                    os.killpg(int(t["pid"]), signal.SIGTERM)
                    stopped += 1
                except OSError:
                    pass
        base.save_state("tunnels", st)
        return {"ok": True, "stopped": stopped, "summary": f"stopped {stopped} tunnel(s)"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return _err("E_ARGS", "port: the local port to expose")
    if not 1 <= port <= 65535:
        return _err("E_ARGS", "port out of range")
    if not port_listening(port):
        return _err("E_PORT_DOWN", f"nothing is listening on {port}: start it first (service_ensure)")
    if not confirmed:
        return _err("E_CONFIRM", f"this makes port {port} reachable from the internet for {minutes} min; "
                    "resend with confirmed: true", needs_confirmation=True)
    minutes = max(1, min(int(minutes or 30), 24 * 60))
    use = provider if provider in ("cloudflared", "ssh") else ("cloudflared" if base.which("cloudflared") else "ssh")
    if use == "cloudflared":
        cmd = f"timeout {minutes * 60} cloudflared tunnel --no-autoupdate --url http://127.0.0.1:{port}"
        pattern = _URL_CF
    else:
        if not base.which("ssh"):
            return _err("E_NOT_FOUND_CMD", "install cloudflared or openssh (pkg install cloudflared)")
        cmd = (f"timeout {minutes * 60} ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 "
               f"-o ExitOnForwardFailure=yes -R 80:127.0.0.1:{port} nokey@localhost.run")
        pattern = _URL_SSH
    decision = kernel.gate(cmd, confirmed=True, tool="tunnel")
    if not decision["allow"]:
        return _err(decision["code"], decision["message"])
    log = os.path.join(base.ensure_dir(base.root("tunnels")), f"{port}-{int(time.time())}.log")
    with open(log, "w") as out:
        proc = subprocess.Popen(cmd, shell=True, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                **kernel.popen_kwargs())
    url = ""
    deadline = time.time() + 30
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
        try:
            with open(log, encoding="utf-8", errors="replace") as handle:
                m = pattern.search(handle.read())
        except OSError:
            m = None
        if m:
            url = m.group(0)
            break
    if not url:
        kernel.kill_group(proc)
        tail = _read_small(log)[-400:]
        return _err("E_TUNNEL", "no public URL within 30 s", provider=use, log_tail=tail)
    tid = "tn-" + hashlib.sha1(f"{port}{time.time()}".encode()).hexdigest()[:6]
    st = _tunnels()
    st[tid] = {"pid": proc.pid, "port": port, "url": url, "provider": use, "log": log,
               "expires": time.time() + minutes * 60}
    base.save_state("tunnels", st)
    return {"ok": True, "id": tid, "url": url, "port": port, "provider": use, "minutes": minutes,
            "summary": f"{url} → localhost:{port} for {minutes} min (tunnel action=stop id={tid})"}


_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
_CHROME_TAGS = {"nav", "footer", "header", "aside", "form"}
_BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "br", "tr", "table", "h1", "h2", "h3",
          "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "figcaption"}
_VOID = {"br", "img", "hr", "meta", "link", "input", "source", "area", "base", "col", "embed", "param",
         "track", "wbr"}


def _parse_selector(sel: str):
    sel = str(sel or "").strip()
    if not sel:
        return None
    m = re.match(r"^([a-zA-Z][\w-]*)?(?:#([\w-]+))?(?:\.([\w-]+))?$", sel)
    if not m or not any(m.groups()):
        return "bad"
    return (m.group(1) or "").lower(), m.group(2) or "", m.group(3) or ""


class _Extractor(HTMLParser):
    def __init__(self, selector):
        super().__init__(convert_charrefs=True)
        self.selector = selector
        self.stack = []
        self.title = ""
        self._in_title = False
        self.all, self.main, self.picked = [], [], []
        self.links = 0

    def _matches(self, tag, attrs):
        if not self.selector:
            return False
        t, i, c = self.selector
        a = dict(attrs)
        return ((not t or t == tag) and (not i or a.get("id") == i)
                and (not c or c in (a.get("class") or "").split()))

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self.links += 1
        if tag in _VOID:
            if tag == "br":
                self._text("\n")
            return
        parent = self.stack[-1] if self.stack else ("", False, False, False, False)
        skip = parent[1] or tag in _SKIP_TAGS
        chrome = parent[2] or tag in _CHROME_TAGS
        selected = parent[3] or self._matches(tag, attrs)
        main = parent[4] or tag in ("article", "main")
        self.stack.append((tag, skip, chrome, selected, main))
        if tag in _BLOCK:
            self._text("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in _BLOCK:
            self._text("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        self._text(data)

    def _text(self, data):
        top = self.stack[-1] if self.stack else ("", False, False, False, False)
        if top[1]:
            return
        if top[3]:
            self.picked.append(data)
        if top[2]:
            return
        self.all.append(data)
        if top[4]:
            self.main.append(data)


def _clean_text(parts) -> str:
    text = html.unescape("".join(parts))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def web_fetch(url: str, selector: str = "", max_chars: int = 3000, timeout: int = 20) -> dict:
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s/$.?#].[^\s]*$", url, re.IGNORECASE):
        return _err("E_ARGS", "url: an http(s) address")
    if policy.get().get("network") is False:
        return _err("E_POLICY", "policy: network access is off for this device")
    sel = _parse_selector(selector)
    if sel == "bad":
        return _err("E_ARGS", "selector: tag, #id, .class or tag.class")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android) termux-mcp/0.13",
                                               "Accept": "text/html,application/json,text/plain,*/*"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=max(3, min(int(timeout or 20), 60))) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(3_000_001)
            status = resp.status
            final = resp.geturl()
    except urllib.error.HTTPError as error:
        return _err("E_HTTP", f"HTTP {error.code} {error.reason}", status=error.code)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return _err("E_NETWORK", str(getattr(error, "reason", error))[:200])
    truncated = len(raw) > 3_000_000
    m = re.search(r"charset=([\w-]+)", ctype)
    text = raw[:3_000_000].decode(m.group(1) if m else "utf-8", errors="replace")
    body = {"ok": True, "url": final, "status": status, "type": ctype.split(";")[0], "bytes": len(raw),
            "secs": round(time.time() - start, 2)}
    head = text.lstrip()[:1]
    if "json" in ctype or (head and head in "{[" and "html" not in ctype):
        try:
            data = json.loads(text)
            pretty = json.dumps(data, indent=1, ensure_ascii=False)
            body["json_keys"] = list(data)[:30] if isinstance(data, dict) else f"array[{len(data)}]"
            text_out = pretty
        except ValueError:
            text_out = text
    elif "html" in ctype or "<html" in text[:2000].lower():
        ex = _Extractor(sel)
        try:
            ex.feed(text)
            ex.close()
        except Exception:
            pass
        body["title"] = _clean_text([ex.title])[:200]
        body["links"] = ex.links
        if sel:
            text_out = _clean_text(ex.picked)
            if not text_out:
                return dict(body, ok=False, errors=[{"code": "E_NO_MATCH", "msg": f"nothing matches {selector}"}])
        else:
            main = _clean_text(ex.main)
            text_out = main if len(main) > 200 else _clean_text(ex.all)
    else:
        text_out = text
    body["chars"] = len(text_out)
    body["words"] = len(text_out.split())
    limit = max(200, min(int(max_chars or 3000), 20000))
    body["text"] = text_out[:limit]
    if len(text_out) > limit:
        body["out_ref"] = base.store_output(text_out)
        body["more"] = f"{len(text_out) - limit} more chars: out_read ref={body['out_ref']}"
    if truncated:
        body["truncated"] = True
    body["summary"] = (body.get("title") or final)[:80] + f" — {body['words']} words"
    return body


_READ_SQL = re.compile(r"^\s*(select|with|pragma|explain|values)\b", re.IGNORECASE)


def _num(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _table_text(cols, rows, width=24) -> str:
    def cell(v):
        s = "NULL" if v is None else str(v)
        return s if len(s) <= width else s[:width - 1] + "…"
    grid = [[cell(c) for c in cols]] + [[cell(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in grid) for i in range(len(cols))]
    lines = [" | ".join(v.ljust(widths[i]) for i, v in enumerate(r)).rstrip() for r in grid]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def db_query(path: str, sql: str, limit: int = 50, write: bool = False, confirmed: bool = False) -> dict:
    real = _path(path)
    if not real or not os.path.isfile(real):
        return _err("E_NO_FILE", f"no such file: {path}")
    reason = _policy_path(real, write=bool(write))
    if reason:
        return _err("E_POLICY", reason)
    sql = str(sql or "").strip()
    if not sql:
        return _err("E_ARGS", "sql")
    limit = max(1, min(int(limit or 50), 1000))
    is_csv = real.lower().endswith((".csv", ".tsv"))
    if is_csv and write:
        return _err("E_ARGS", "CSV files are queried read-only (write the result to a new file)")
    deadline = time.time() + 15
    try:
        if is_csv:
            conn = sqlite3.connect(":memory:")
            with open(real, newline="", encoding="utf-8", errors="replace") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel_tab if real.lower().endswith(".tsv") else csv.excel
                reader = csv.reader(handle, dialect)
                header = next(reader, [])
                cols, seen = [], set()
                for i, h in enumerate(header):
                    name = re.sub(r"\W+", "_", h.strip()).strip("_") or f"c{i + 1}"
                    while name.lower() in seen:
                        name += "_"
                    seen.add(name.lower())
                    cols.append(name)
                if not cols:
                    return _err("E_EMPTY", "the CSV has no header row")
                qcols = ", ".join(f'"{c}"' for c in cols)
                conn.execute(f"CREATE TABLE t ({qcols})")
                stem = re.sub(r"\W+", "_", os.path.splitext(os.path.basename(real))[0]).strip("_")
                if stem and stem.lower() != "t" and stem[0].isalpha():
                    conn.execute(f'CREATE VIEW "{stem}" AS SELECT * FROM t')
                ph = ", ".join("?" * len(cols))
                n = 0
                for row in reader:
                    row = (row + [""] * len(cols))[:len(cols)]
                    conn.execute(f"INSERT INTO t VALUES ({ph})", [_num(v) for v in row])
                    n += 1
                    if n > 500_000:
                        break
        else:
            if write:
                if not confirmed:
                    return _err("E_CONFIRM", "writing to the database; resend with confirmed: true",
                                needs_confirmation=True)
                safety.snapshot_before_write(real, tool="db_query", cmd=sql[:200])
                conn = sqlite3.connect(real, timeout=5)
            else:
                if not _READ_SQL.match(sql):
                    return _err("E_READ_ONLY", "read-only query: SELECT/WITH/PRAGMA (pass write: true to change)")
                conn = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
        cur = conn.execute(sql)
        if cur.description is None:
            conn.commit()
            changed = conn.total_changes
            conn.close()
            return {"ok": True, "changed": changed, "summary": f"{changed} row(s) changed"}
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        conn.close()
    except sqlite3.DatabaseError as error:
        msg = str(error)
        code = "E_TIMEOUT" if "interrupted" in msg else "E_SQL"
        return _err(code, msg[:300], **({"hint": "not an SQLite database"} if "file is not a database" in msg else {}))
    except (OSError, UnicodeError) as error:
        return _err("E_IO", str(error)[:200])
    more = len(rows) > limit
    rows = [list(r) for r in rows[:limit]]
    table = _table_text(cols, rows)
    body = {"ok": True, "columns": cols, "rows": rows, "count": len(rows), "more": more,
            "summary": f"{len(rows)}{'+' if more else ''} row(s) × {len(cols)} column(s)"}
    if is_csv:
        body["table_name"] = "t"
    if len(table) > base.INLINE_CHARS:
        body["out_ref"] = base.store_output(table)
        body["rows"] = rows[:10]
        body["note"] = f"first 10 rows inline; full table: out_read ref={body['out_ref']}"
    else:
        body["table"] = table
    return body


def log_watch(path: str, pattern: str, action: str = "scan", notify: str = "", cmd: str = "",
              name: str = "", lines: int = 400, id: str = "", confirmed: bool = False) -> dict:
    from . import reactor
    action = action or "scan"
    if action == "stop":
        if not id:
            return _err("E_ARGS", "id: the rule id from log_watch/watch_list")
        return reactor.remove(id)
    real = _path(path)
    if not real:
        return _err("E_ARGS", "path: the log file")
    reason = _policy_path(real)
    if reason:
        return _err("E_POLICY", reason)
    try:
        rx = re.compile(str(pattern or ""), re.IGNORECASE)
    except re.error as error:
        return _err("E_ARGS", f"pattern: {error}")
    if not pattern:
        return _err("E_ARGS", "pattern: a regex to look for")
    if action == "scan":
        if not os.path.isfile(real):
            return _err("E_NO_FILE", f"no such file: {path}")
        size = os.path.getsize(real)
        with open(real, "rb") as handle:
            handle.seek(max(0, size - 512_000))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()[-max(10, min(int(lines or 400), 5000)):]
        hits = [ln[:300] for ln in tail if rx.search(ln)]
        return {"ok": True, "path": real, "scanned": len(tail), "matches": len(hits), "last": hits[-20:],
                "summary": f"{len(hits)} match(es) in the last {len(tail)} line(s)"}
    if action == "watch":
        if not (notify or cmd):
            notify = f"{os.path.basename(real)}: {{match}}"
        act = {}
        if notify:
            act["notify"] = notify
        if cmd:
            act["cmd"] = cmd
        return reactor.add({"log_match": {"path": real, "pattern": pattern}}, act,
                           name or f"log {os.path.basename(real)}", confirmed)
    return _err("E_ARGS", "action: scan|watch|stop")


INDEX_TTL = 600
INDEX_MAX = 200_000


def _index_file(root: str) -> str:
    return os.path.join(base.ensure_dir(base.root("index")), hashlib.sha1(root.encode()).hexdigest()[:12] + ".tsv")


def _build_index(root: str) -> list:
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        for f in filenames:
            p = os.path.join(dirpath, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            rows.append((st.st_size, int(st.st_mtime), os.path.relpath(p, root)))
            if len(rows) >= INDEX_MAX:
                return rows
    return rows


def _index(root: str, refresh: bool = False):
    path = _index_file(root)
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < INDEX_TTL
    if fresh and not refresh:
        rows = []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for ln in handle:
                parts = ln.rstrip("\n").split("\t", 2)
                if len(parts) == 3:
                    rows.append((int(parts[0]), int(parts[1]), parts[2]))
        return rows, True
    rows = _build_index(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for size, mtime, rel in rows:
            if "\n" not in rel and "\t" not in rel:
                handle.write(f"{size}\t{mtime}\t{rel}\n")
    os.replace(tmp, path)
    return rows, False


def file_find(query: str = "", path: str = "~", kind: str = "name", ext: str = "", newer_than_days: float = 0,
              larger_than_mb: float = 0, limit: int = 30, refresh: bool = False) -> dict:
    import fnmatch
    root = _path(path, "~")
    if not os.path.isdir(root):
        return _err("E_NO_FILE", f"not a folder: {path}")
    reason = _policy_path(root)
    if reason:
        return _err("E_POLICY", reason)
    limit = max(1, min(int(limit or 30), 500))
    query = str(query or "").strip()
    exts = [e.lower().lstrip(".") for e in re.split(r"[,\s]+", str(ext or "")) if e]
    rows, cached = _index(root, bool(refresh))
    now = time.time()

    def keep(size, mtime, rel):
        if exts and rel.rsplit(".", 1)[-1].lower() not in exts:
            return False
        if newer_than_days and now - mtime > float(newer_than_days) * 86400:
            return False
        if larger_than_mb and size < float(larger_than_mb) * 1e6:
            return False
        return True

    hits = []
    if kind == "content":
        if not query:
            return _err("E_ARGS", "query: text to search for")
        if base.which("rg"):
            globs = " ".join(f"-g {q('!' + d)}" for d in sorted(SKIP_DIRS))
            res = base.sh(f"rg -l -i -F --max-filesize 5M {globs} -- {q(query)} {q(root)} 2>/dev/null | head -2000",
                          timeout=60, check_risk=False)
            sizes = {rel: (s, m) for s, m, rel in rows}
            for line in res.out.splitlines():
                rel = os.path.relpath(line, root)
                s, m = sizes.get(rel, (0, 0))
                if not sizes.get(rel):
                    try:
                        st = os.stat(line)
                        s, m = st.st_size, int(st.st_mtime)
                    except OSError:
                        continue
                if keep(s, m, rel):
                    hits.append((0, s, m, rel))
        else:
            needle = query.lower().encode()
            stop_at = time.time() + 20
            for s, m, rel in rows:
                if time.time() > stop_at or len(hits) >= 2000:
                    break
                if s > 2_000_000 or not keep(s, m, rel):
                    continue
                try:
                    with open(os.path.join(root, rel), "rb") as handle:
                        data = handle.read()
                except OSError:
                    continue
                if b"\0" in data[:1024]:
                    continue
                if needle in data.lower():
                    hits.append((0, s, m, rel))
    else:
        ql = query.lower()
        glob = any(ch in query for ch in "*?[")
        for s, m, rel in rows:
            if not keep(s, m, rel):
                continue
            name = os.path.basename(rel).lower()
            if not query:
                rank = 3
            elif glob:
                if not fnmatch.fnmatch(name, ql):
                    continue
                rank = 1
            elif name == ql:
                rank = 0
            elif name.startswith(ql):
                rank = 1
            elif ql in name:
                rank = 2
            elif ql in rel.lower():
                rank = 3
            else:
                continue
            hits.append((rank, s, m, rel))
        if not query and (larger_than_mb or not newer_than_days):
            hits.sort(key=lambda h: -h[1])
        elif not query:
            hits.sort(key=lambda h: -h[2])
    if query or kind == "content":
        hits.sort(key=lambda h: (h[0], -h[2]))
    total = len(hits)
    out = [{"path": os.path.join(root, rel), "size": s,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m))} for _, s, m, rel in hits[:limit]]
    body = {"ok": True, "root": root, "count": total, "files": out, "indexed": len(rows), "index_cached": cached,
            "summary": f"{total} match(es)" + (f", showing {limit}" if total > limit else "")}
    if total > limit:
        body["out_ref"] = base.store_output("\n".join(os.path.join(root, h[3]) for h in hits))
    return body


BACKUP_SKIP = {"node_modules", "__pycache__", ".cache", "termuxGPT", ".npm", ".termux-mcp", "storage",
               ".gradle", ".pub-cache"}


def _backup_walk(src: str, skip: str):
    skip_abs = {skip, os.path.join(base.home(), "backups"), os.path.join(base.home(), "restored")}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in BACKUP_SKIP
                       and os.path.join(dirpath, d) not in skip_abs
                       and not os.path.islink(os.path.join(dirpath, d))]
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            yield p


def _obj_path(dest: str, sha: str) -> str:
    return os.path.join(dest, "objects", sha[:2], sha + ".z")


def _snapshots(dest: str) -> list:
    folder = os.path.join(dest, "snapshots")
    try:
        return sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        return []


def _load_snap(dest: str, sid: str):
    with open(os.path.join(dest, "snapshots", sid + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def _restic(action, src, dest, snapshot, target, password, confirmed):
    pw = os.path.join(base.ensure_dir(base.root("state")), ".restic-pw")
    fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(password)
    try:
        r = f"restic -r {q(dest)} --password-file {q(pw)}"
        if action == "backup":
            if not os.path.exists(os.path.join(dest, "config")):
                res, body = _run(f"{r} init", timeout=120)
                if not res.ok:
                    return dict(body, ok=False)
            excludes = " ".join(f"--exclude {q(d)}" for d in ("node_modules", ".cache", "__pycache__"))
            res, body = _run(f"{r} backup {excludes} {q(src)}", timeout=7200)
        elif action == "list":
            res, body = _run(f"{r} snapshots --compact", timeout=120)
        elif action == "verify":
            res, body = _run(f"{r} check", timeout=3600)
        else:
            if not confirmed:
                return _err("E_CONFIRM", "restore writes files; resend with confirmed: true", needs_confirmation=True)
            res, body = _run(f"{r} restore {q(snapshot or 'latest')} --target {q(target)}", timeout=7200,
                             confirmed=True)
        body.update(engine="restic", encrypted=True)
        return body
    finally:
        try:
            os.remove(pw)
        except OSError:
            pass


def backup_incremental(action: str = "backup", src: str = "~", dest: str = "", snapshot: str = "",
                       target: str = "", password: str = "", full: bool = False, confirmed: bool = False) -> dict:
    action = action or "backup"
    if action not in ("backup", "list", "verify", "restore"):
        return _err("E_ARGS", "action: backup|list|verify|restore")
    src = _path(src, "~")
    dest = _path(dest) if dest else os.path.join(base.home(), "backups", os.path.basename(src.rstrip("/")) or "home")
    if action == "restore":
        target = _path(target) if target else ""
    reason = _policy_path(src) or _policy_path(dest, write=action == "backup") or \
        (_policy_path(target, write=True) if target else "")
    if reason:
        return _err("E_POLICY", reason)
    if password and base.which("restic"):
        if action == "restore" and not target:
            return _err("E_ARGS", "target: where to restore (a new folder is safest)")
        return _restic(action, src, dest, snapshot, target, str(password), confirmed)
    if password and not base.which("restic"):
        return _err("E_NOT_FOUND_CMD", "encrypted backups use restic: pkg install restic (or omit password)")
    if action == "list":
        snaps = _snapshots(dest)
        rows = []
        for sid in snaps[-30:]:
            try:
                m = _load_snap(dest, sid)
                rows.append({"id": sid, "files": len(m["files"]), "bytes": m.get("bytes", 0), "src": m.get("src")})
            except (OSError, ValueError, KeyError):
                rows.append({"id": sid, "broken": True})
        return {"ok": True, "dest": dest, "snapshots": rows, "engine": "builtin", "encrypted": False,
                "summary": f"{len(snaps)} snapshot(s) in {dest}"}
    if action == "backup":
        if not os.path.isdir(src):
            return _err("E_NO_FILE", f"not a folder: {src}")
        if dest == src:
            return _err("E_ARGS", "dest must not be the source folder")
        os.makedirs(os.path.join(dest, "snapshots"), exist_ok=True)
        prev = {}
        snaps = _snapshots(dest)
        if snaps:
            try:
                prev = _load_snap(dest, snaps[-1]).get("files", {})
            except (OSError, ValueError):
                prev = {}
        files, new_objects, reused, total, stored = {}, 0, 0, 0, 0
        started = time.time()
        for p in _backup_walk(src, dest):
            rel = os.path.relpath(p, src)
            try:
                st = os.stat(p)
            except OSError:
                continue
            total += st.st_size
            old = prev.get(rel)
            if old and old["size"] == st.st_size and old["mtime"] == int(st.st_mtime) \
                    and os.path.exists(_obj_path(dest, old["sha"])):
                files[rel] = old
                reused += 1
                continue
            try:
                with open(p, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            sha = hashlib.sha256(data).hexdigest()
            obj = _obj_path(dest, sha)
            if not os.path.exists(obj):
                os.makedirs(os.path.dirname(obj), exist_ok=True)
                packed = zlib.compress(data, 6)
                with open(obj + ".tmp", "wb") as handle:
                    handle.write(packed)
                os.replace(obj + ".tmp", obj)
                new_objects += 1
                stored += len(packed)
            files[rel] = {"sha": sha, "size": st.st_size, "mtime": int(st.st_mtime), "mode": st.st_mode & 0o777}
        sid = time.strftime("%Y%m%d-%H%M%S")
        while sid in snaps:
            sid += "b"
        manifest = {"id": sid, "src": src, "created": base.now_iso(), "bytes": total, "files": files}
        with open(os.path.join(dest, "snapshots", sid + ".json.tmp"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.replace(os.path.join(dest, "snapshots", sid + ".json.tmp"), os.path.join(dest, "snapshots", sid + ".json"))
        return {"ok": True, "snapshot": sid, "dest": dest, "files": len(files), "new_objects": new_objects,
                "unchanged": reused, "bytes": total, "stored_bytes": stored, "secs": round(time.time() - started, 1),
                "engine": "builtin", "encrypted": False,
                "summary": f"snapshot {sid}: {len(files)} file(s), {new_objects} new object(s), "
                           f"{reused} unchanged" + ("" if base.which("restic") else
                                                     " (unencrypted; pass password with restic installed to encrypt)")}
    snaps = _snapshots(dest)
    if not snaps:
        return _err("E_NO_BACKUP", f"no snapshots in {dest}")
    sid = snapshot or snaps[-1]
    if sid not in snaps:
        return _err("E_NO_BACKUP", f"no snapshot {sid}", available=snaps[-10:])
    manifest = _load_snap(dest, sid)
    if action == "verify":
        bad, checked = [], 0
        for rel, f in manifest["files"].items():
            obj = _obj_path(dest, f["sha"])
            checked += 1
            if not os.path.exists(obj):
                bad.append({"file": rel, "problem": "missing object"})
                continue
            if full:
                try:
                    with open(obj, "rb") as handle:
                        data = zlib.decompress(handle.read())
                    if hashlib.sha256(data).hexdigest() != f["sha"]:
                        bad.append({"file": rel, "problem": "checksum mismatch"})
                except (OSError, zlib.error):
                    bad.append({"file": rel, "problem": "unreadable object"})
        return {"ok": not bad, "snapshot": sid, "checked": checked, "full": bool(full), "problems": bad[:30],
                "errors": [{"code": "E_BACKUP_CORRUPT", "msg": f"{len(bad)} problem(s)"}] if bad else [],
                "summary": f"{sid}: {checked} file(s) {'fully ' if full else ''}verified, {len(bad)} problem(s)"}
    target = target or os.path.join(base.home(), "restored",
                                    f"{os.path.basename(manifest['src'].rstrip('/')) or 'home'}-{sid}")
    in_place = os.path.isdir(target) and any(True for _ in os.scandir(target))
    if in_place and not confirmed:
        return _err("E_CONFIRM", f"{target} is not empty: existing files would be overwritten (they are "
                    "snapshotted first); resend with confirmed: true", needs_confirmation=True, target=target)
    restored = 0
    for rel, f in manifest["files"].items():
        out = os.path.normpath(os.path.join(target, rel))
        if not out.startswith(target.rstrip("/") + "/"):
            continue
        try:
            with open(_obj_path(dest, f["sha"]), "rb") as handle:
                data = zlib.decompress(handle.read())
        except (OSError, zlib.error):
            continue
        if os.path.exists(out):
            safety.snapshot_before_write(out, tool="backup_incremental")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as handle:
            handle.write(data)
        try:
            os.chmod(out, f.get("mode", 0o644))
            os.utime(out, (f["mtime"], f["mtime"]))
        except OSError:
            pass
        restored += 1
    return {"ok": restored == len(manifest["files"]), "snapshot": sid, "target": target, "restored": restored,
            "of": len(manifest["files"]), "summary": f"restored {restored}/{len(manifest['files'])} file(s) to {target}"}


def clipboard_pipe(direction: str = "get", text: str = "", file: str = "", max_chars: int = 2000) -> dict:
    direction = direction or "get"
    if not base.which("termux-clipboard-get"):
        return _err("E_NO_API", "Termux:API is needed (pkg install termux-api + the Termux:API app)")
    if direction in ("get", "to_file"):
        res = base.sh("termux-clipboard-get", check_risk=False)
        if not res.ok:
            return _err("E_TIMEOUT" if res.timed_out else "E_API", res.text.strip()[:200] or "clipboard read failed")
        value = res.out
        if direction == "to_file":
            real = _path(file)
            if not real:
                return _err("E_ARGS", "file: where to save the clipboard")
            reason = _policy_path(real, write=True)
            if reason:
                return _err("E_POLICY", reason)
            safety.snapshot_before_write(real, tool="clipboard_pipe")
            os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
            with open(real, "w", encoding="utf-8") as handle:
                handle.write(value)
            return {"ok": True, "file": real, "chars": len(value), "summary": f"saved {len(value)} chars to {real}"}
        body = {"ok": True, "chars": len(value), "text": value[:max(100, int(max_chars or 2000))],
                "summary": f"clipboard: {len(value)} chars"}
        if len(value) > len(body["text"]):
            body["out_ref"] = base.store_output(value)
        return body
    if direction == "set":
        if file:
            real = _path(file)
            reason = _policy_path(real)
            if reason:
                return _err("E_POLICY", reason)
            try:
                with open(real, encoding="utf-8", errors="replace") as handle:
                    text = handle.read(1_000_001)
            except OSError as error:
                return _err("E_NO_FILE", str(error))
        if not text:
            return _err("E_ARGS", "text or file")
        res = base.sh("termux-clipboard-set", stdin=str(text)[:1_000_000], check_risk=False)
        if not res.ok:
            return _err("E_API", res.text.strip()[:200] or "clipboard write failed")
        return {"ok": True, "chars": len(text), "summary": f"copied {len(text)} chars to the clipboard"}
    return _err("E_ARGS", "direction: get|set|to_file")


def share_to(path: str = "", text: str = "", url: str = "", action: str = "send", title: str = "") -> dict:
    action = action if action in ("send", "view", "edit") else "send"
    if url:
        if not re.match(r"^https?://\S+$", url):
            return _err("E_ARGS", "url: http(s) only")
        res = base.sh(f"termux-open-url {q(url)}", check_risk=False)
        return {"ok": res.ok, "summary": f"opened {url}" if res.ok else "could not open the URL",
                **({} if res.ok else {"errors": [{"code": "E_API", "msg": res.text.strip()[:200]}]})}
    if not base.which("termux-share"):
        return _err("E_NO_API", "Termux:API is needed (pkg install termux-api)")
    t = f" -t {q(title)}" if title else ""
    if path:
        real = _path(path)
        if not os.path.isfile(real):
            return _err("E_NO_FILE", f"no such file: {path}")
        reason = _policy_path(real)
        if reason:
            return _err("E_POLICY", reason)
        res = base.sh(f"termux-share -a {action}{t} {q(real)}", check_risk=False)
        what = os.path.basename(real)
    elif text:
        res = base.sh(f"termux-share -a send{t}", stdin=str(text)[:100_000], check_risk=False)
        what = f"{len(text)} chars"
    else:
        return _err("E_ARGS", "path, text or url")
    if not res.ok:
        return _err("E_TIMEOUT" if res.timed_out else "E_API", res.text.strip()[:200] or "share failed")
    return {"ok": True, "summary": f"share sheet opened for {what}"}


PRESETS = {
    "audio": "-vn -c:a libmp3lame -q:a 2",
    "audio_copy": "-vn -c:a copy",
    "compress": "-c:v libx264 -preset veryfast -crf {crf} -c:a aac -b:a 128k -movflags +faststart",
    "mp4": "-c:v libx264 -preset veryfast -crf 23 -c:a aac -movflags +faststart",
    "gif": "",
    "thumbnail": "-frames:v 1 -q:v 2",
    "resize": "-vf scale={width}:-2 -c:a copy",
    "trim": "-c copy",
}


def _probe_duration(src: str) -> float:
    res = base.sh(f"ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 {q(src)}", timeout=30,
                  check_risk=False)
    try:
        return float(res.out.strip())
    except ValueError:
        return 0.0


def _progress_file(output: str) -> str:
    return os.path.join(base.ensure_dir(base.root("media")), hashlib.sha1(output.encode()).hexdigest()[:10] + ".progress")


def media_convert(input: str = "", output: str = "", preset: str = "", start: str = "", duration: str = "",
                  crf: int = 28, width: int = 480, fps: int = 12, overwrite: bool = False, action: str = "convert",
                  background: bool = False) -> dict:
    out = _path(output)
    if action == "status":
        pf = _progress_file(out)
        state = base.load_state("media_jobs", {}).get(out, {})
        pct = None
        try:
            with open(pf, encoding="utf-8") as handle:
                text = handle.read()
            times = re.findall(r"out_time_ms=(\d+)", text)
            if times and state.get("duration"):
                pct = min(100.0, round(int(times[-1]) / 1e6 / state["duration"] * 100, 1))
            if "progress=end" in text:
                pct = 100.0
        except OSError:
            pass
        return {"ok": True, "output": out, "percent": pct, "status": state.get("status", "unknown"),
                "summary": f"{pct if pct is not None else '?'}% ({state.get('status', 'unknown')})"}
    src = _path(input)
    if not src or not os.path.isfile(src):
        return _err("E_NO_FILE", f"no such file: {input}")
    if not out:
        return _err("E_ARGS", "output: the file to write (its extension picks the container)")
    reason = _policy_path(src) or _policy_path(out, write=True)
    if reason:
        return _err("E_POLICY", reason)
    if not base.which("ffmpeg"):
        return _err("E_NOT_FOUND_CMD", "ffmpeg is not installed", fixes=[{"id": "pkg_install", "steps": ["pkg install -y ffmpeg"]}])
    ext = out.rsplit(".", 1)[-1].lower() if "." in out else ""
    if not preset:
        preset = {"mp3": "audio", "m4a": "audio_copy", "aac": "audio_copy", "gif": "gif", "jpg": "thumbnail",
                  "jpeg": "thumbnail", "png": "thumbnail"}.get(ext, "mp4")
    if preset not in PRESETS:
        return _err("E_ARGS", f"preset: {', '.join(PRESETS)}")
    if os.path.exists(out) and not overwrite:
        return _err("E_EXISTS", f"{out} exists; pass overwrite: true (the old file is snapshotted)")
    if background:
        from . import tasks, run_smart_tool
        params = {"input": input, "output": output, "preset": preset, "start": start, "duration": duration,
                  "crf": crf, "width": width, "fps": fps, "overwrite": overwrite}
        started = tasks.start("media_convert", params, run_smart_tool)
        return dict(started, output=out, progress=f"media_convert action=status output={out}")
    try:
        crf, width, fps = int(crf or 28), int(width or 480), int(fps or 12)
    except (TypeError, ValueError):
        return _err("E_ARGS", "crf/width/fps must be numbers")
    for v in (start, duration):
        if v and not re.match(r"^\d+(\.\d+)?$|^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$", str(v)):
            return _err("E_ARGS", "start/duration: seconds or HH:MM:SS")
    safety.snapshot_before_write(out, tool="media_convert")
    if preset == "gif":
        graph = f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"
        opts = f"-filter_complex {q(graph)} -loop 0"
    else:
        opts = PRESETS[preset].format(crf=crf, width=width, fps=fps)
    seek = (f"-ss {start} " if start else ("-ss 1 " if preset == "thumbnail" else ""))
    dur = f"-t {duration} " if duration else ""
    total = _probe_duration(src)
    pf = _progress_file(out)
    jobs = base.load_state("media_jobs", {})
    jobs[out] = {"status": "running", "duration": float(duration) if str(duration).replace(".", "").isdigit()
                 else total, "started": base.now_iso()}
    base.save_state("media_jobs", jobs)
    cmd = (f"ffmpeg -hide_banner -nostdin -loglevel error -y {seek}-i {q(src)} {dur}{opts} "
           f"-progress {q(pf)} {q(out)}")
    res, body = _run(cmd, timeout=3600)
    jobs = base.load_state("media_jobs", {})
    jobs[out] = dict(jobs.get(out, {}), status="done" if res.ok else "failed")
    base.save_state("media_jobs", jobs)
    if not res.ok:
        return dict(body, ok=False, preset=preset)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    return {"ok": size > 0, "output": out, "preset": preset, "bytes": size, "input_bytes": os.path.getsize(src),
            "secs": body.get("secs"),
            "summary": f"{preset}: {os.path.basename(src)} → {os.path.basename(out)} "
                       f"({size // 1024} KB, was {os.path.getsize(src) // 1024} KB)"}


HOST_OK = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[?[0-9a-fA-F:]+\]?$")
USER_OK = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.IGNORECASE)


def _ssh_config_hosts() -> list:
    path = os.path.join(base.home(), ".ssh", "config")
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for ln in handle:
                m = re.match(r"\s*Host\s+(.+)", ln, re.IGNORECASE)
                if m:
                    out += [h for h in m.group(1).split() if "*" not in h and "?" not in h]
    except OSError:
        pass
    return out


def ssh_hosts(action: str = "list", name: str = "", host: str = "", user: str = "", port: int = 22,
              key: str = "") -> dict:
    action = action or "list"
    hosts = base.load_state("ssh_hosts", {})
    if action == "list":
        rows = [{"name": n, **h} for n, h in hosts.items()]
        cfg = [h for h in _ssh_config_hosts() if h not in hosts]
        return {"ok": True, "hosts": rows, "ssh_config": cfg,
                "summary": f"{len(rows)} saved host(s)" + (f", {len(cfg)} in ~/.ssh/config" if cfg else "")}
    if not NAME_OK.match(str(name or "")):
        return _err("E_ARGS", "name: letters, digits, . _ -")
    if action == "remove":
        existed = hosts.pop(name, None) is not None
        base.save_state("ssh_hosts", hosts)
        return {"ok": True, "removed": existed, "summary": f"{'removed' if existed else 'no host'} {name}"}
    if action == "add":
        if not HOST_OK.match(str(host or "")):
            return _err("E_ARGS", "host: a hostname or IP")
        if user and not USER_OK.match(user):
            return _err("E_ARGS", "user: a unix user name")
        try:
            port = int(port or 22)
        except (TypeError, ValueError):
            return _err("E_ARGS", "port: a number")
        entry = {"host": host, "user": user or "", "port": port}
        if key:
            real = _path(key)
            if not os.path.isfile(real):
                return _err("E_NO_FILE", f"no key at {key}")
            entry["key"] = real
        hosts[name] = entry
        base.save_state("ssh_hosts", hosts)
        return {"ok": True, "name": name, **entry, "summary": f"saved {name} ({user + '@' if user else ''}{host}:{port})"}
    return _err("E_ARGS", "action: list|add|remove")


def ssh_run(host: str, cmd: str, timeout: int = 60, confirmed: bool = False) -> dict:
    if not base.which("ssh"):
        return _err("E_NOT_FOUND_CMD", "pkg install openssh")
    cmd = str(cmd or "").strip()
    if not cmd:
        return _err("E_ARGS", "cmd: what to run on the remote machine")
    saved = base.load_state("ssh_hosts", {}).get(str(host or ""))
    opts = ["-o BatchMode=yes", "-o ConnectTimeout=8", "-o StrictHostKeyChecking=accept-new"]
    if saved:
        target = (saved["user"] + "@" if saved.get("user") else "") + saved["host"]
        if saved.get("port") and int(saved["port"]) != 22:
            opts.append(f"-p {int(saved['port'])}")
        if saved.get("key"):
            opts.append(f"-i {q(saved['key'])}")
    else:
        m = re.match(r"^(?:([^@\s]+)@)?([^@\s:]+)(?::(\d+))?$", str(host or ""))
        if not m or not HOST_OK.match(m.group(2)) or (m.group(1) and not USER_OK.match(m.group(1))):
            return _err("E_ARGS", "host: a saved name (ssh_hosts) or user@host[:port]")
        target = (m.group(1) + "@" if m.group(1) else "") + m.group(2)
        if m.group(3):
            opts.append(f"-p {int(m.group(3))}")
    full = f"ssh {' '.join(opts)} {q(target)} -- {q(cmd)}"
    res, body = _run(full, timeout=int(timeout or 60), confirmed=confirmed)
    body["host"] = target
    body["cmd"] = cmd
    if res.exit == 255:
        body["ok"] = False
        body["errors"] = [{"code": "E_SSH", "msg": (res.err.strip().splitlines() or ["ssh failed"])[-1][:200]}]
        body["hint"] = "key auth only (BatchMode): ssh-copy-id first, or save a key with ssh_hosts"
    return body
