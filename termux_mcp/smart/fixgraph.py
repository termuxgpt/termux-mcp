import re
import threading

from . import base, digest

FIXES = [
    ("pkg_update_index", "E_PKG_NOT_FOUND", "", ["pkg update -y"], "apt-cache policy {subject} | grep -q Candidate", "write",
     "Refresh the package index, then the package may appear"),
    ("pkg_search_name", "E_PKG_NOT_FOUND", "", ["pkg search '^{subject}' 2>/dev/null | head -20"], "", "safe",
     "Search for the right package name"),
    ("pkg_change_mirror", "E_PKG_FETCH", "", ["termux-change-repo --auto 2>/dev/null || true", "pkg update -y"],
     "pkg update -y", "write", "Switch to a working mirror and refresh"),
    ("pkg_clean_lists", "E_PKG_FETCH", r"Hash Sum mismatch|Clearsigned",
     ["rm -rf $PREFIX/var/lib/apt/lists/*", "pkg update -y"], "pkg update -y", "write",
     "Drop corrupt package lists and re-download"),
    ("dpkg_wait_lock", "E_DPKG_LOCK", "", ["sleep 5", "pgrep -a apt || pgrep -a dpkg || true"],
     "! pgrep -x apt >/dev/null && ! pgrep -x dpkg >/dev/null", "safe",
     "Wait for the other package operation to finish"),
    ("dpkg_configure", "E_DPKG_BROKEN", "", ["dpkg --configure -a", "apt --fix-broken install -y"],
     "dpkg --audit | wc -l | grep -qx 0", "write", "Finish interrupted package setup"),
    ("pip_break_system", "E_PIP_EXTERNAL", "", ["{cmd} --break-system-packages"], "", "write",
     "Retry pip with --break-system-packages (Termux has no system Python to break)"),
    ("pip_upgrade_tools", "E_PIP_BUILD", "", ["pip install -U pip setuptools wheel --break-system-packages"],
     "", "write", "Upgrade pip build tooling"),
    ("pip_build_deps", "E_PIP_BUILD", "", ["pkg install -y clang make pkg-config libffi openssl rust binutils"],
     "command -v clang", "write", "Install the native build toolchain"),
    ("pip_termux_wheel", "E_PIP_NO_DIST", r"numpy|scipy|pandas|opencv|matplotlib|lxml|pillow|cryptography",
     ["pkg install -y python-{subject_lower} 2>/dev/null || pkg search python-{subject_lower}"],
     "python -c 'import {subject_lower}'", "write", "Use the Termux-packaged build instead of PyPI"),
    ("py_module_pip", "E_PY_MODULE", "", ["pip install {subject} --break-system-packages"],
     "python -c 'import {subject}'", "write", "Install the missing Python module"),
    ("py_module_pkg", "E_PY_MODULE", r"numpy|cryptography|lxml|pillow|yaml|psutil",
     ["pkg install -y python-{subject_lower}"], "python -c 'import {subject}'", "write",
     "Install the Termux-packaged module"),
    ("node_module_install", "E_NODE_MODULE", "", ["npm install {subject}"], "", "write",
     "Install the missing node module"),
    ("npm_check_name", "E_NPM_404", "", ["npm search {subject} --parseable 2>/dev/null | head -5"], "", "safe",
     "Look up the correct npm package name"),
    ("cmd_install_pkg", "E_NOT_FOUND_CMD", "", ["pkg install -y {subject}"], "command -v {subject}", "write",
     "Install the command's package"),
    ("cmd_which_pkg", "E_NOT_FOUND_CMD", "", ["pkg search '^{subject}' 2>/dev/null | head -10"], "", "safe",
     "Find which package provides the command"),
    ("crlf_fix", "E_CRLF", "", ["sed -i 's/\\r$//' {subject_file}"], "! grep -q $'\\r' {subject_file}", "write",
     "Strip Windows line endings from the script"),
    ("shebang_fix", "E_SHEBANG", "", ["termux-fix-shebang {subject_file}"], "", "write",
     "Point the shebang at Termux's interpreter path"),
    ("lib_reinstall", "E_LIB_MISSING", "", ["pkg upgrade -y", "pkg install -y --reinstall $(dpkg -S {subject} 2>/dev/null | cut -d: -f1 | head -1)"],
     "", "write", "Upgrade packages so the shared library matches"),
    ("arch_note", "E_ARCH", "", ["uname -m", "file {subject_file} 2>/dev/null || true"], "", "safe",
     "Show CPU arch vs binary arch (needs an aarch64/arm build)"),
    ("port_who", "E_PORT_IN_USE", "", ["(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep ':{subject} '"], "",
     "safe", "Show what is holding the port"),
    ("port_free_kill", "E_PORT_IN_USE", "", ["fuser -k {subject}/tcp 2>/dev/null || (lsof -ti tcp:{subject} | xargs -r kill)"],
     "! (ss -ltn 2>/dev/null | grep -q ':{subject} ')", "destructive", "Stop the process holding the port"),
    ("space_pkg_cache", "E_NO_SPACE", "", ["apt clean", "pip cache purge 2>/dev/null || true", "npm cache clean --force 2>/dev/null || true"],
     "", "write", "Clear package caches to free space"),
    ("space_report", "E_NO_SPACE", "", ["df -h $HOME", "du -sh $HOME/* 2>/dev/null | sort -rh | head -10"], "", "safe",
     "Show what is using space"),
    ("storage_setup", "E_STORAGE", "", ["termux-setup-storage"], "test -d ~/storage/shared", "write",
     "Grant Termux access to shared storage"),
    ("perm_chmod_exec", "E_PERMISSION", r"\./|\.sh", ["chmod +x {subject_file}"], "test -x {subject_file}", "write",
     "Make the script executable"),
    ("perm_home_copy", "E_PERMISSION", r"/sdcard|/storage", ["cp {subject_file} $HOME/ && echo copied to $HOME"], "",
     "write", "Shared storage is noexec; copy the file into $HOME"),
    ("net_dns_check", "E_NETWORK", "", ["ping -c1 -W3 1.1.1.1 >/dev/null && echo ip-ok || echo no-ip",
                                         "getent hosts github.com || nslookup github.com 2>/dev/null | head -5"], "",
     "safe", "Tell DNS failures apart from no connectivity"),
    ("net_resolv", "E_NETWORK", r"resolve", ["printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > $PREFIX/etc/resolv.conf"],
     "getent hosts github.com", "write", "Use public DNS servers"),
    ("ssl_ca", "E_SSL", "", ["pkg install -y ca-certificates openssl"], "", "write", "Refresh CA certificates"),
    ("git_safe_dir", "E_GIT_NOT_REPO", r"dubious ownership", ["git config --global --add safe.directory '*'"], "", "write",
     "Trust repos on shared storage"),
    ("git_init_hint", "E_GIT_NOT_REPO", "", ["git rev-parse --show-toplevel 2>&1; ls -d */.git 2>/dev/null | head"], "",
     "safe", "Find the actual repo folder"),
    ("git_auth_https", "E_GIT_AUTH", "", ["git config --global credential.helper store",
                                          "echo 'Use a GitHub token as the password (Settings > Developer settings)'"], "",
     "write", "Store credentials; GitHub needs a token, not a password"),
    ("git_auth_ssh", "E_GIT_AUTH", r"publickey", ["test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519",
                                                  "cat ~/.ssh/id_ed25519.pub"], "", "write",
     "Create an SSH key and show the public key to add on GitHub"),
    ("git_stash_pull", "E_GIT_CONFLICT", r"would be overwritten", ["git stash", "git pull --ff-only", "git stash pop"],
     "git diff --name-only --diff-filter=U | wc -l | grep -qx 0", "write", "Stash local edits, pull, re-apply"),
    ("git_rebase_pull", "E_GIT_DIVERGED", "", ["git pull --rebase"], "", "write", "Rebase local commits on top of remote"),
    ("termux_api_install", "E_TERMUX_API", "", ["pkg install -y termux-api"], "command -v termux-battery-status", "write",
     "Install the termux-api package (also install the Termux:API app, same source as Termux)"),
    ("timeout_api_hint", "E_TIMEOUT", r"termux-", ["pkg install -y termux-api"], "", "write",
     "termux-api calls hang when the Termux:API app is missing or lacks permission"),
    ("killed_memory", "E_KILLED", "", ["free -m", "ps -eo pid,rss,comm --sort=-rss | head -8"], "", "safe",
     "Show memory pressure; close apps or use smaller jobs"),
    ("syntax_python2", "E_SYNTAX", r"print ", ["pkg install -y python2 2>/dev/null; python2 {subject_file}"], "", "write",
     "Script is Python 2; run it with python2"),
]

_lock = threading.Lock()
_STATE = "fixgraph"


def _stats():
    return base.load_state(_STATE, {"fixes": {}, "failures": []})


def _rate(stats, fid):
    row = stats["fixes"].get(fid)
    if not row:
        return None
    return round(row["ok"] / max(row["tries"], 1), 2)


def recent_failures(n=3) -> list:
    return _stats().get("failures", [])[-n:]


def note_failure(code: str, cmd: str) -> None:
    with _lock:
        st = _stats()
        st.setdefault("failures", []).append({"code": code, "cmd": cmd[:80], "at": base.now_iso()})
        st["failures"] = st["failures"][-30:]
        base.save_state(_STATE, st)


def _subject_file(cmd: str) -> str:
    for tok in reversed(cmd.split()):
        if "/" in tok or tok.endswith((".sh", ".py", ".js")):
            return tok
    return cmd.split()[-1] if cmd.split() else ""


def _fill(template: str, subject: str, cmd: str) -> str:
    safe_subject = re.sub(r"[^\w.+@/:-]", "", subject)[:80]
    return (template.replace("{subject_lower}", safe_subject.lower().split(".")[0])
            .replace("{subject_file}", _subject_file(cmd))
            .replace("{subject}", safe_subject).replace("{cmd}", cmd))


def lookup(error_text: str = "", code: str = "", cmd: str = "", subject: str = "") -> dict:
    errors = []
    if code:
        errors = [{"code": code, "subject": subject}]
    elif error_text:
        errors = digest.classify(error_text)
    if not errors:
        return {"ok": True, "matched": False, "fixes": [],
                "summary": "no known fix — hand this to the model"}
    stats = _stats()
    found = []
    for err in errors:
        c = err["code"]
        subj = subject or err.get("subject", "")
        for fid, fcode, extra, cmds, verify, risk, desc in FIXES:
            if fcode != c:
                continue
            if extra and not re.search(extra, f"{error_text}\n{cmd}\n{subj}", re.IGNORECASE):
                continue
            rate = _rate(stats, fid)
            found.append({"id": fid, "code": c, "risk": risk, "desc": desc,
                          "steps": [_fill(x, subj, cmd) for x in cmds],
                          **({"verify": _fill(verify, subj, cmd)} if verify else {}),
                          "success_rate": rate if rate is not None else 0.8,
                          "_subject": subj})
    try:
        from . import learn
        for err in errors:
            found.extend(learn.learned_for(err["code"], error_text))
            cand = learn.candidate_for(err["code"], error_text)
            if cand:
                found.append(cand)
    except Exception:
        pass
    found.sort(key=lambda f: (f["risk"] != "safe", -f["success_rate"]))
    for f in found:
        f.pop("_subject", None)
    return {"ok": True, "matched": bool(found), "errors": errors, "fixes": found[:5],
            "summary": f"{len(found)} known fix(es) for {errors[0]['code']}" if found
            else f"no known fix for {errors[0]['code']}"}


def apply(fix_id: str, subject: str = "", cmd: str = "", confirmed: bool = False,
          cwd: str = "") -> dict:
    spec = next((f for f in FIXES if f[0] == fix_id), None)
    learned = None
    if spec is None:
        from . import learn
        learned = learn.learned_spec(fix_id) or learn._fixes()["candidates"].get(fix_id)
        if learned:
            spec = (learned["id"], learned["code"], "", list(learned["steps"]), learned.get("verify", ""),
                    learned.get("risk", "write"), learned.get("desc", ""))
    if spec is None:
        return {"ok": False, "errors": [{"code": "E_NO_FIX", "msg": f"unknown fix {fix_id}"}]}
    fid, code, _, cmds, verify, risk, desc = spec
    if risk != "safe" and not confirmed:
        return {"ok": False, "needs_confirmation": True, "risk": risk,
                "steps": [_fill(c, subject, cmd) for c in cmds],
                "errors": [{"code": "E_CONFIRM", "msg": f"{fid} is {risk}; resend with confirmed: true"}]}
    steps = []
    for template in cmds:
        c = _fill(template, subject, cmd)
        res = base.sh(c, timeout=300, cwd=cwd or None, confirmed=True)
        d = digest.from_shell(res)
        steps.append({"cmd": c, **{k: v for k, v in d.items() if k in ("ok", "exit", "summary", "errors", "out", "excerpt")}})
        if not res.ok and verify:
            break
    fixed = None
    if verify:
        v = base.sh(_fill(verify, subject, cmd), timeout=120, cwd=cwd or None, check_risk=False)
        fixed = v.ok
    else:
        fixed = all(s.get("ok") for s in steps)
    if learned is not None:
        from . import learn
        if fid in learn._fixes()["promoted"]:
            learn.learned_record(fid, bool(fixed))
        else:
            learn.fix_learn(code=code, fix=list(learned["steps"]), verify=learned.get("verify", ""),
                            worked=bool(fixed), error=learned.get("sample", ""))
    with _lock:
        st = _stats()
        row = st["fixes"].setdefault(fid, {"tries": 0, "ok": 0})
        row["tries"] += 1
        row["ok"] += 1 if fixed else 0
        base.save_state(_STATE, st)
    return {"ok": bool(fixed), "fix": fid, "verified": verify != "", "steps": steps,
            "success_rate": _rate(_stats(), fid),
            "summary": f"{fid}: {'fixed' if fixed else 'did not fix it'}"}
