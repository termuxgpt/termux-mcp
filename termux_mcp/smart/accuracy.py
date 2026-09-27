import os
import re
import shlex
import uuid

from .. import changes, safety
from ..security import get_risk_assessment
from . import base

INTERACTIVE = {"vim", "vi", "nano", "htop", "top", "less", "more", "man", "fzf",
               "lazygit", "tmux", "ssh", "mysql", "psql", "sqlite3", "python", "python3",
               "node", "irb", "bc", "ftp", "telnet", "emacs", "mc", "ranger", "cmatrix"}


def _expand_braces(token: str) -> list:
    m = re.search(r"\{([^{}]*,[^{}]*)\}", token)
    if not m:
        rng = re.search(r"\{(\d+)\.\.(\d+)\}", token)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if abs(b - a) <= 100:
                step = 1 if b >= a else -1
                return [token[:rng.start()] + str(i) + token[rng.end():] for i in range(a, b + step, step)]
        return [token]
    out = []
    for part in m.group(1).split(","):
        out.extend(_expand_braces(token[:m.start()] + part + token[m.end():]))
    return out


def shell_lint(cmd: str) -> dict:
    original = str(cmd or "")
    fixed = original
    issues = []

    def fix_braces(match):
        word = match.group(0)
        parts = _expand_braces(word)
        return " ".join(parts) if len(parts) > 1 else word
    scripted = re.search(r"\b(awk|sed|jq|perl|python3?\s+-c|node\s+-e|find\b.*-exec)\b", fixed)
    if re.search(r"\S*\{[^{}\s]*(,|\.\.)[^{}\s]*\}\S*", fixed) and "${" not in fixed and not scripted:
        new = re.sub(r"[^\s'\"]*\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}[^\s'\"]*", fix_braces, fixed)
        if new != fixed:
            issues.append({"code": "L_BRACE", "msg": "sh does not expand {a,b}; expanded explicitly"})
            fixed = new

    m = re.match(r"^\s*cd\s+(\S+)\s*&&\s*(.+)$", fixed)
    if m:
        target, rest = m.group(1), m.group(2)
        first = rest.split()[0] if rest.split() else ""
        if first == "git":
            fixed = f"git -C {target} {rest[3:].strip()}"
            issues.append({"code": "L_CD_CHAIN", "msg": "rewrote 'cd X && git …' as 'git -C X …'"})
        else:
            fixed = f"(cd {target} && {rest})"
            issues.append({"code": "L_CD_CHAIN", "msg": "wrapped cd chain in a subshell"})

    try:
        words = shlex.split(fixed)
    except ValueError:
        words = fixed.split()
        issues.append({"code": "L_QUOTES", "msg": "unbalanced quotes"})
    head = os.path.basename(words[0]) if words else ""
    if head in INTERACTIVE and (len(words) == 1 or head in {"vim", "vi", "nano", "htop", "top", "less",
                                                             "more", "man", "fzf", "lazygit", "tmux",
                                                             "cmatrix", "mc", "ranger", "emacs"}):
        issues.append({"code": "L_INTERACTIVE", "msg": f"{head} needs a terminal; use terminal_open"})

    if re.search(r"\b(pkg|apt|apt-get)\s+(install|upgrade|remove|uninstall)\b", fixed) \
            and not re.search(r"\s-y\b|--yes\b", fixed):
        fixed = re.sub(r"\b(pkg|apt|apt-get)\s+(install|upgrade|remove|uninstall)\b", r"\1 \2 -y", fixed, count=1)
        issues.append({"code": "L_NO_YES", "msg": "added -y so it cannot wait on a prompt"})

    if re.match(r"^\s*sudo\s+", fixed):
        fixed = re.sub(r"^\s*sudo\s+", "", fixed)
        issues.append({"code": "L_SUDO", "msg": "Termux has no sudo; removed it"})

    if re.search(r"\bpip3?\s+install\b", fixed) and "--break-system-packages" not in fixed \
            and "-r " not in fixed and "--user" not in fixed:
        fixed = re.sub(r"(\bpip3?\s+install\b[^;&|]*)", r"\1 --break-system-packages", fixed, count=1)
        issues.append({"code": "L_PIP_PEP668", "msg": "added --break-system-packages"})

    if re.search(r"(>|\btee\b|\brm\b|\bmv\b|\bcp\b)\s+/(system|proc|sys|dev)(?!/null)", fixed):
        issues.append({"code": "L_SYSTEM_PATH", "msg": "touches a system path"})

    risk = get_risk_assessment(fixed)
    return {"ok": not any(i["code"] in ("L_QUOTES", "L_INTERACTIVE") for i in issues),
            "cmd": fixed, "changed": fixed != original, "issues": issues,
            "risk": risk["risk_level"],
            "blocked": risk["blocked"], "needs_confirmation": risk["requires_confirmation"],
            "summary": ("rewritten: " + fixed) if fixed != original else
            ("ok" if not issues else "; ".join(i["msg"] for i in issues))}


def plan_check(steps) -> dict:
    report = []
    ok = True
    from . import intent_tools
    for i, step in enumerate(steps or []):
        if isinstance(step, str):
            step = {"cmd": step}
        entry = {"i": i}
        if step.get("cmd"):
            lint = shell_lint(step["cmd"])
            entry.update(cmd=lint["cmd"], issues=lint["issues"], blocked=lint["blocked"],
                         needs_confirmation=lint["needs_confirmation"])
            words = lint["cmd"].split()
            if words:
                exe = words[0]
                if exe not in ("cd", "export", "echo", "(", "[", "test", "true", "false") \
                        and not exe.startswith(("./", "/", "~", "$", "(")) and not base.which(exe):
                    entry.setdefault("problems", []).append({"code": "E_NOT_FOUND_CMD", "subject": exe})
            for path in re.findall(r"(?:^|\s)((?:~|/)[\w@./+-]+)", lint["cmd"]):
                real = os.path.expanduser(path)
                reads = re.search(rf"(cat|less|head|tail|python3?|node|bash|sh|source|cp|mv)\s+\S*{re.escape(path)}",
                                  lint["cmd"])
                if reads and not os.path.exists(real):
                    entry.setdefault("problems", []).append({"code": "E_NO_FILE", "subject": path})
            m = re.search(r"(?:--port[ =]|-p\s+|:)(\d{4,5})\b", lint["cmd"])
            if m and re.search(r"serve|server|http|run|start|listen", lint["cmd"]):
                if intent_tools.port_listening(int(m.group(1))):
                    entry.setdefault("problems", []).append({"code": "E_PORT_IN_USE", "subject": m.group(1)})
            pk = re.search(r"\b(?:pkg|apt)\s+install\s+(?:-y\s+)?([\w.+ -]+)", lint["cmd"])
            if pk:
                for name in pk.group(1).split():
                    if name.startswith("-"):
                        continue
                    res = base.sh(f"apt-cache policy {shlex.quote(name)} 2>/dev/null | grep -q 'Candidate: [^(]'",
                                  timeout=10, check_risk=False)
                    if res.exit == 1:
                        entry.setdefault("problems", []).append({"code": "E_PKG_NOT_FOUND", "subject": name})
        elif step.get("tool"):
            from . import SMART_TOOLS
            entry["tool"] = step["tool"]
            if step["tool"] not in SMART_TOOLS:
                entry["note"] = "not a smart tool; validated by the server at call time"
        if entry.get("blocked") or entry.get("problems"):
            ok = False
        report.append(entry)
    problems = sum(len(e.get("problems", [])) + (1 if e.get("blocked") else 0) for e in report)
    return {"ok": ok, "steps": report,
            "summary": "plan looks runnable" if ok else f"{problems} problem(s) found before running"}


def tx_begin(title: str = "") -> dict:
    tx = "tx-" + uuid.uuid4().hex[:10]
    state = base.load_state("tx", {})
    state[tx] = {"title": title[:80], "started": base.now_iso(), "status": "open"}
    base.save_state("tx", dict(list(state.items())[-100:]))
    return {"ok": True, "tx": tx, "task_id": tx,
            "summary": f"transaction {tx} open — pass task_id: {tx} to every write"}


def _tx_entries(tx: str) -> list:
    root = safety.safety_root("")
    return [e for e in changes.read(root, limit=2000, task=tx)]


def tx_status(tx: str) -> dict:
    state = base.load_state("tx", {})
    entries = _tx_entries(tx)
    return {"ok": True, "tx": tx, **state.get(tx, {"status": "unknown"}), "changes": len(entries),
            "files": [e.get("path") for e in entries[:30]],
            "summary": f"{tx}: {len(entries)} change(s)"}


def tx_commit(tx: str) -> dict:
    state = base.load_state("tx", {})
    if tx in state:
        state[tx]["status"] = "committed"
        base.save_state("tx", state)
    return {"ok": True, "tx": tx, "changes": len(_tx_entries(tx)), "summary": f"{tx} committed"}


def tx_rollback(tx: str, confirmed: bool = False) -> dict:
    entries = _tx_entries(tx)
    if not entries:
        return {"ok": True, "tx": tx, "summary": "nothing to roll back"}
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "changes": len(entries),
                "files": [e.get("path") for e in entries[:30]],
                "errors": [{"code": "E_CONFIRM", "msg": "resend with confirmed: true"}]}
    done = changes.revert(entries, snapshot_before=lambda p: safety.snapshot_before_write(
        p, tool="tx_rollback"))
    state = base.load_state("tx", {})
    if tx in state:
        state[tx]["status"] = "rolled_back"
        base.save_state("tx", state)
    restored = sum(1 for _, r in done if r in ("restored", "removed"))
    return {"ok": True, "tx": tx, "reverted": restored,
            "results": [{"path": p, "result": r} for p, r in done[:40]],
            "summary": f"rolled back {restored} of {len(done)} change(s)"}


_CLAIM_PATTERNS = [
    ("version", re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?)\b(?!\s?(?:KB|MB|GB|KiB|MiB|GiB|B\b|%))", re.IGNORECASE)),
    ("path", re.compile(r"(?:~|/)[\w@.+-]+(?:/[\w@.+-]+)+")),
    ("size", re.compile(r"\b\d+(?:\.\d+)?\s?(?:KB|MB|GB|KiB|MiB|GiB|B)\b", re.IGNORECASE)),
    ("percent", re.compile(r"\b\d+(?:\.\d+)?%")),
    ("port", re.compile(r"(?:port|:)\s?(\d{2,5})\b", re.IGNORECASE)),
]
_ACTION_CLAIMS = re.compile(r"\b(installed|removed|deleted|created|updated|upgraded|started|stopped|"
                            r"cloned|fixed|restored|killed|freed|scheduled|written|saved)\b", re.IGNORECASE)


def answer_check(answer: str, evidence=None, actions=None) -> dict:
    corpus = "\n".join(str(x) for x in (evidence or [])) + "\n" + "\n".join(str(x) for x in (actions or []))
    corpus_low = corpus.lower()
    unsupported = []
    for kind, rx in _CLAIM_PATTERNS:
        for m in rx.finditer(answer or ""):
            token = m.group(1) if m.groups() else m.group(0)
            if token.lower() not in corpus_low:
                unsupported.append({"kind": kind, "claim": token})
    verbs = {m.group(1).lower() for m in _ACTION_CLAIMS.finditer(answer or "")}
    if verbs and not actions and not evidence:
        unsupported.append({"kind": "action", "claim": ", ".join(sorted(verbs))})
    elif verbs and actions:
        acted = " ".join(str(a) for a in actions).lower()
        roots = {"installed": ("install", "pkg_ensure"), "removed": ("uninstall", "remove", "rm", "delete"),
                 "deleted": ("delete", "rm", "trash"), "cloned": ("clone",), "started": ("service_ensure", "start"),
                 "stopped": ("kill", "stop", "port_free"), "scheduled": ("cron", "watch"),
                 "written": ("write", "file_edit", ">"), "restored": ("undo", "rollback", "restore"),
                 "freed": ("storage_clean", "clean", "rm ", "trash", "port_free"),
                 "fixed": ("fix_apply", "fix", "file_edit"), "killed": ("kill", "port_free", "service_stop"),
                 "created": ("write", "mkdir", "touch", "file_edit", ">"), "updated": ("update", "upgrade", "file_edit", "pull"),
                 "upgraded": ("upgrade", "install"), "saved": ("write", "save", "snapshot", "file_edit")}
        for v in verbs:
            keys = roots.get(v)
            if keys and not any(k in acted for k in keys):
                unsupported.append({"kind": "action", "claim": v})
    seen = set()
    uniq = []
    for u in unsupported:
        key = (u["kind"], u["claim"])
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    return {"ok": not uniq, "grounded": not uniq, "unsupported": uniq[:15],
            "summary": "every claim is backed by a tool result" if not uniq
            else f"{len(uniq)} claim(s) not backed by any tool result — remove or verify them"}
