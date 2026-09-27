import re

from . import base

ERROR_RULES = [
    ("E_TIMEOUT", r"^__TIMEOUT__$", "the command did not finish in time"),
    ("E_BLOCKED", r"^__BLOCKED__$", "blocked by the safety filter"),
    ("E_CONFIRM", r"^__CONFIRM__$", "needs confirmed: true"),
    ("E_TERMUX_API", r"termux-api.*(not (installed|found)|no such file)|"
                     r"Termux:API.*not (installed|found)",
     "Termux:API app or package missing"),
    ("E_DPKG_LOCK", r"Could not get lock|dpkg.*lock|Unable to acquire the dpkg",
     "another package operation holds the lock"),
    ("E_DPKG_BROKEN", r"dpkg was interrupted|you must manually run 'dpkg --configure -a'",
     "package database interrupted"),
    ("E_PKG_NOT_FOUND", r"Unable to locate package\s+(\S+)|E: Package '([^']+)' has no installation candidate",
     "no such package in the repo"),
    ("E_PKG_FETCH", r"Failed to fetch|Temporary failure resolving|Some index files failed to download|"
                    r"Hash Sum mismatch|Clearsigned file isn't valid",
     "package mirror / index problem"),
    ("E_PIP_EXTERNAL", r"externally-managed-environment",
     "pip blocked by PEP 668"),
    ("E_PIP_NO_DIST", r"No matching distribution found for\s+(\S+)|Could not find a version that satisfies",
     "package not on PyPI for this platform"),
    ("E_PIP_BUILD", r"Failed building wheel for\s+(\S+)|error: command '.*(clang|gcc)' failed",
     "native build failed"),
    ("E_PY_MODULE", r"ModuleNotFoundError: No module named '([^']+)'",
     "python module missing"),
    ("E_NPM_404", r"npm ERR! 404|npm error 404", "npm package not found"),
    ("E_NODE_MODULE", r"Cannot find module '([^']+)'", "node module missing"),
    ("E_GIT_AUTH", r"Authentication failed|could not read Username|Permission denied \(publickey\)",
     "git authentication failed"),
    ("E_GIT_NOT_REPO", r"not a git repository", "not inside a git repo"),
    ("E_GIT_CONFLICT", r"CONFLICT \(|Automatic merge failed|would be overwritten by merge",
     "merge conflict / dirty tree"),
    ("E_GIT_DIVERGED", r"Not possible to fast-forward|have diverged", "branches diverged"),
    ("E_CRLF", r"\^M|/usr/bin/env: .*\\r|bad interpreter: .*\r", "Windows line endings"),
    ("E_SHEBANG", r"bad interpreter: No such file or directory|/usr/bin/env: .+: No such file",
     "script shebang points at a missing interpreter"),
    ("E_LIB_MISSING", r"error while loading shared libraries: (\S+)|CANNOT LINK EXECUTABLE.*library \"([^\"]+)\"",
     "shared library missing"),
    ("E_ARCH", r"Exec format error|cannot execute binary file", "binary built for another CPU"),
    ("E_PORT_IN_USE", r"Address already in use|EADDRINUSE|port (\d+) is already",
     "the port is taken"),
    ("E_NO_SPACE", r"No space left on device|ENOSPC", "storage is full"),
    ("E_STORAGE", r"(/sdcard|/storage/emulated|~/storage).*Permission denied",
     "shared storage not granted"),
    ("E_PERMISSION", r"Permission denied|EACCES|Operation not permitted",
     "permission denied"),
    ("E_NETWORK", r"Could not resolve host|Name or service not known|Network is unreachable|"
                  r"Connection timed out|Temporary failure in name resolution",
     "network / DNS problem"),
    ("E_SSL", r"SSL certificate problem|CERTIFICATE_VERIFY_FAILED", "TLS certificate problem"),
    ("E_SYNTAX", r"SyntaxError:|syntax error near unexpected token|unexpected EOF",
     "syntax error"),
    ("E_NOT_FOUND_CMD", r"(?:^|: )(\S+): (?:command )?not found|No command (\S+) found",
     "command not installed"),
    ("E_NO_FILE", r"No such file or directory", "path does not exist"),
    ("E_KILLED", r"^Killed$|signal 9|Out of memory", "killed (memory)"),
]

_COMPILED = [(c, re.compile(p, re.IGNORECASE | re.MULTILINE), h)
             for c, p, h in ERROR_RULES]


def classify(text: str, exit_code: int = 1) -> list:
    found = []
    seen = set()
    for code, regex, hint in _COMPILED:
        m = regex.search(text or "")
        if not m or code in seen:
            continue
        subject = next((g for g in m.groups() if g), "") if m.groups() else ""
        item = {"code": code, "msg": hint}
        if subject:
            item["subject"] = subject.strip("'\"")[:80]
        line = _line_of(text, m.start())
        if line:
            item["line"] = line[:200]
        found.append(item)
        seen.add(code)
        if code == "E_PERMISSION" and "E_STORAGE" in seen:
            found.pop()
        if len(found) >= 3:
            break
    if not found and exit_code not in (0, None):
        last = _last_meaningful(text)
        found.append({"code": "E_EXIT", "msg": f"exit status {exit_code}",
                      **({"line": last[:200]} if last else {})})
    return found


def _line_of(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end >= 0 else len(text)].strip()


def _last_meaningful(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _p_pkg_install(cmd, text):
    facts = {}
    new = re.search(r"(\d+) upgraded, (\d+) newly installed, (\d+) to remove", text)
    if new:
        facts.update(upgraded=int(new.group(1)), installed=int(new.group(2)),
                     removed=int(new.group(3)))
    setting = re.findall(r"Setting up ([\w.+-]+) \(([^)]+)\)", text)
    if setting:
        facts["packages"] = {n: v for n, v in setting[:30]}
    already = re.findall(r"([\w.+-]+) is already the newest version \(([^)]+)\)", text)
    if already:
        facts.setdefault("packages", {}).update({n: v for n, v in already[:30]})
        facts["already"] = [n for n, _ in already[:30]]
    if "packages" in facts:
        pk = facts["packages"]
        summary = ", ".join(f"{n} {v}" for n, v in list(pk.items())[:6])
        return f"installed/present: {summary}", facts
    if new:
        return (f"{facts['installed']} installed, {facts['upgraded']} upgraded,"
                f" {facts['removed']} removed"), facts
    return "", facts


def _p_pkg_update(cmd, text):
    m = re.search(r"(\d+) packages? can be upgraded", text)
    if m:
        return f"index updated; {m.group(1)} upgradable", {"upgradable": int(m.group(1))}
    if re.search(r"All packages are up to date", text):
        return "index updated; everything up to date", {"upgradable": 0}
    return _p_pkg_install(cmd, text)


def _p_pip(cmd, text):
    ok = re.search(r"Successfully installed (.+)", text)
    if ok:
        pkgs = dict(re.findall(r"([\w.\-]+)-([\d][\w.]*)", ok.group(1)))
        return "pip installed " + ", ".join(f"{k} {v}" for k, v in list(pkgs.items())[:6]), \
            {"packages": pkgs}
    sat = re.findall(r"Requirement already satisfied: ([\w.\-]+)", text)
    if sat:
        return f"already satisfied: {', '.join(sat[:6])}", {"already": sat[:30]}
    return "", {}


def _p_npm(cmd, text):
    m = re.search(r"added (\d+) packages?", text)
    if m:
        return f"npm added {m.group(1)} packages", {"added": int(m.group(1))}
    m = re.search(r"up to date", text)
    if m:
        return "npm: up to date", {}
    return "", {}


def _p_git_clone(cmd, text):
    m = re.search(r"Cloning into '([^']+)'", text)
    if m:
        return f"cloned into {m.group(1)}", {"path": m.group(1)}
    return "", {}


def _p_git_status(cmd, text):
    lines = [l for l in text.splitlines() if l.strip()]
    branch = ""
    head = next((l for l in lines if l.startswith("## ")), "")
    if head:
        branch = head[3:]
    changed = [l for l in lines if not l.startswith("## ")]
    facts = {"branch": branch, "changed": len(changed),
             "files": [l[3:] for l in changed[:20]]}
    return f"{branch or 'repo'}: {len(changed)} changed", facts


def _p_df(cmd, text):
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            rows.append({"fs": parts[0], "size": parts[1], "used": parts[2],
                         "avail": parts[3], "use": parts[4], "mount": parts[5]})
    if rows:
        main = next((r for r in rows if r["mount"] in ("/data", "/storage/emulated")), rows[-1])
        return f"{main['mount']}: {main['avail']} free of {main['size']} ({main['use']} used)", \
            {"disks": rows[:8]}
    return "", {}


def _p_du(cmd, text):
    rows = []
    for line in text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        if len(parts) == 2:
            rows.append({"size": parts[0].strip(), "path": parts[1].strip()})
    if rows:
        return "largest: " + ", ".join(f"{r['path']} {r['size']}" for r in rows[:5]), \
            {"entries": rows[:25]}
    return "", {}


def _p_ls(cmd, text):
    names = [l for l in text.splitlines() if l.strip() and not l.startswith("total ")]
    return f"{len(names)} entries", {"count": len(names), "entries": names[:40]}


def _p_ps(cmd, text):
    lines = [l for l in text.splitlines() if l.strip()]
    return f"{max(len(lines) - 1, 0)} processes", {"top": lines[1:11]}


def _p_version(cmd, text):
    m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[\w.-]*)?)", text)
    if m:
        tool = cmd.split()[0]
        return f"{tool} {m.group(1)}", {"version": m.group(1)}
    return "", {}


def _p_test(cmd, text):
    m = re.search(r"(\d+) passed", text)
    f = re.search(r"(\d+) failed", text)
    if m or f:
        facts = {"passed": int(m.group(1)) if m else 0,
                 "failed": int(f.group(1)) if f else 0}
        return f"tests: {facts['passed']} passed, {facts['failed']} failed", facts
    return "", {}


def _p_compile(cmd, text):
    errs = re.findall(r"^(.+?):(\d+):(\d+): error: (.+)$", text, re.MULTILINE)
    if errs:
        items = [{"file": a, "line": int(b), "msg": d[:120]} for a, b, _, d in errs[:10]]
        return f"{len(errs)} compile errors", {"compile_errors": items}
    return "", {}


PARSERS = [
    (re.compile(r"^(pkg|apt|apt-get) (install|reinstall|upgrade)\b"), _p_pkg_install),
    (re.compile(r"^(pkg|apt|apt-get) update\b"), _p_pkg_update),
    (re.compile(r"^(pip3?|python3? -m pip) install\b"), _p_pip),
    (re.compile(r"^npm (i|install|ci)\b"), _p_npm),
    (re.compile(r"^git clone\b"), _p_git_clone),
    (re.compile(r"^git status\b.*(-s|--short|--porcelain)"), _p_git_status),
    (re.compile(r"^df\b"), _p_df),
    (re.compile(r"^du\b"), _p_du),
    (re.compile(r"^ls\b"), _p_ls),
    (re.compile(r"^ps\b"), _p_ps),
    (re.compile(r"^(pytest|python3? -m pytest|npm test|cargo test|go test)\b"), _p_test),
    (re.compile(r"^(gcc|g\+\+|clang|clang\+\+|make|cargo build)\b"), _p_compile),
    (re.compile(r"^\S+ (--version|-V|version)$"), _p_version),
]


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r(?!\n)", "", text or "")


def excerpt(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) <= base.EXCERPT_HEAD + base.EXCERPT_TAIL:
        return "\n".join(lines)[:base.INLINE_CHARS]
    head = lines[:base.EXCERPT_HEAD]
    tail = lines[-base.EXCERPT_TAIL:]
    return "\n".join(head + [f"… {len(lines) - len(head) - len(tail)} lines …"] + tail)[:base.INLINE_CHARS]


def make(cmd: str, exit_code: int, out: str, err: str = "", *,
         timed_out: bool = False, blocked: str = "", elapsed: float = 0.0) -> dict:
    raw = _strip_ansi(out if not err else (out.rstrip("\n") + "\n" + err))
    body = {"ok": exit_code == 0 and not timed_out and not blocked,
            "exit": exit_code}
    if elapsed:
        body["secs"] = round(elapsed, 2)
    if blocked:
        code = {"blocked": "E_BLOCKED", "denied": "E_POLICY"}.get(blocked, "E_CONFIRM")
        body["errors"] = [{"code": code, "msg": err.strip()[:200] or code}]
        body["ok"] = False
        return body
    if timed_out:
        body["errors"] = [{"code": "E_TIMEOUT", "msg": "did not finish in time"}]

    summary, facts = "", {}
    first = cmd.strip()
    for regex, parser in PARSERS:
        if regex.search(first):
            try:
                summary, facts = parser(first, raw)
            except Exception:
                summary, facts = "", {}
            break
    if not body["ok"]:
        body.setdefault("errors", []).extend(
            e for e in classify(raw, exit_code)
            if e["code"] not in {x["code"] for x in body.get("errors", [])})
    if summary:
        body["summary"] = summary
    if facts:
        body["facts"] = facts
    if len(raw) > base.INLINE_CHARS:
        body["excerpt"] = excerpt(raw)
        ref = base.store_output(raw)
        if ref:
            body["out_ref"] = ref
        body["out_bytes"] = len(raw)
    else:
        body["out"] = raw.strip()
    return body


def from_shell(res) -> dict:
    return make(res.cmd, res.exit, res.out, res.err, timed_out=res.timed_out,
                blocked=res.blocked, elapsed=res.elapsed)


def read_output(ref: str, grep: str = "", head: int = 0, tail: int = 0,
                lines: str = "", max_chars: int = 4000) -> dict:
    text = base.load_output(ref)
    if text is None:
        return {"ok": False, "errors": [{"code": "E_NO_REF", "msg": f"no stored output {ref}"}]}
    rows = text.splitlines()
    total = len(rows)
    if grep:
        try:
            rx = re.compile(grep, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(grep), re.IGNORECASE)
        rows = [f"{i + 1}: {r}" for i, r in enumerate(rows) if rx.search(r)]
    elif lines:
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", str(lines))
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            rows = rows[max(a - 1, 0):b]
    elif head:
        rows = rows[:int(head)]
    elif tail:
        rows = rows[-int(tail):]
    else:
        rows = rows[:80]
    out = "\n".join(rows)
    truncated = len(out) > max_chars
    return {"ok": True, "ref": ref, "total_lines": total, "shown": len(rows),
            "text": out[:max_chars], **({"truncated": True} if truncated else {})}
