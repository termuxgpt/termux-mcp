import datetime
import json
import os
import re
import shlex
import shutil
import subprocess

JOURNAL_KEEP = 2000
JOURNAL_MAX_BYTES = 400_000

CREATE = "create"
MODIFY = "modify"
DELETE = "delete"
PKG_INSTALL = "pkg_install"
PKG_REMOVE = "pkg_remove"
PACKAGE_KINDS = (PKG_INSTALL, PKG_REMOVE)

_PKG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")
_UNDO = {
    ("pkg", PKG_INSTALL): "pkg uninstall -y {}",
    ("pkg", PKG_REMOVE): "pkg install -y {}",
    ("pip", PKG_INSTALL): "pip uninstall -y {}",
    ("pip", PKG_REMOVE): "pip install {}",
    ("npm", PKG_INSTALL): "npm uninstall -g {}",
    ("npm", PKG_REMOVE): "npm install -g {}",
}


def _names(tokens: list) -> list:
    out = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-r", "--requirement", "-e", "--editable", "-c", "--constraint", "-t", "--target", "-i", "--index-url"):
            skip_next = True
            continue
        if tok.startswith("-") or "/" in tok or tok.endswith((".whl", ".tar.gz", ".deb", ".zip")):
            continue
        name = re.split(r"[=<>!~\[@;]", tok, maxsplit=1)[0]
        if _NAME_OK.match(name):
            out.append(name)
    return out


def parse_package_ops(cmd: str) -> list:
    ops = []
    for segment in _PKG_SPLIT.split(cmd or ""):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        while tokens and (tokens[0] in ("sudo", "yes", "time", "nohup") or "=" in tokens[0] and not tokens[0].startswith("-")):
            tokens = tokens[1:]
        if len(tokens) < 3:
            continue
        head, verb, rest = tokens[0], tokens[1], tokens[2:]
        if head in ("pkg", "apt", "apt-get"):
            manager = "pkg"
            kind = PKG_INSTALL if verb in ("install", "in", "i") else PKG_REMOVE if verb in ("uninstall", "remove", "purge", "rm") else None
        elif head in ("pip", "pip3") or (head.startswith("python") and verb == "-m" and rest and rest[0] in ("pip", "pip3")):
            if head.startswith("python"):
                verb, rest = (rest[1], rest[2:]) if len(rest) > 1 else ("", [])
            manager = "pip"
            kind = PKG_INSTALL if verb == "install" else PKG_REMOVE if verb == "uninstall" else None
            if kind == PKG_INSTALL and any(t in ("-U", "--upgrade") for t in rest):
                kind = None
        elif head == "npm" and any(t in ("-g", "--global") for t in rest):
            manager = "npm"
            kind = PKG_INSTALL if verb in ("install", "i", "add") else PKG_REMOVE if verb in ("uninstall", "remove", "rm", "un") else None
        else:
            continue
        names = _names(rest)
        if kind and names:
            ops.append((kind, manager, names))
    return ops


def _dpkg_installed(names: list) -> set:
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${Status}\n", *names],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split()[0] for line in r.stdout.splitlines() if line.endswith("install ok installed")}


def record_packages(root: str, cmd: str, task_id: str = "", installed=None) -> list:
    recorded = []
    for kind, manager, names in parse_package_ops(cmd):
        if manager == "pkg":
            have = (installed or _dpkg_installed)(names)
            names = [n for n in names if (n not in have) == (kind == PKG_INSTALL)]
        if not names:
            continue
        record(root, kind, f"{manager}:{' '.join(names)}", tool="run", cmd=cmd,
               task_id=task_id, manager=manager, packages=names)
        recorded.append((kind, manager, names))
    return recorded


def undo_command(entry: dict) -> str:
    template = _UNDO.get((str(entry.get("manager", "")), str(entry.get("kind", ""))))
    names = [n for n in entry.get("packages") or [] if _NAME_OK.match(str(n))]
    if not template or not names:
        return ""
    return template.format(" ".join(shlex.quote(n) for n in names))


def _default_root() -> str:
    from .safety import safety_root
    return safety_root("")


def _undone_path(root: str) -> str:
    return os.path.join(root, "undone.json")


def undone(root: str) -> set:
    try:
        with open(_undone_path(root), encoding="utf-8") as handle:
            return set(json.load(handle))
    except (OSError, ValueError):
        return set()


def mark_undone(root: str, ts: str) -> None:
    done = undone(root)
    done.add(ts)
    try:
        os.makedirs(root, exist_ok=True)
        with open(_undone_path(root), "w", encoding="utf-8") as handle:
            json.dump(sorted(done)[-JOURNAL_KEEP:], handle)
    except OSError:
        pass

def journal_path(root: str) -> str:
    return os.path.join(root, "changes.jsonl")

_CREDENTIAL_PATTERNS = (
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 ***"),
    (re.compile(r"(?i)\b(authorization)\b\s*[:=]?\s*\S+"), r"\1 ***"),
    (re.compile(r"(?i)\b(token|api[_-]?key|apikey|password|passwd|secret)"
                r"\b\s*[:=]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)(--?(?:password|token|api[_-]?key|secret))\s+\S+"),
     r"\1 ***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***:***@"),
    (re.compile(r"(?i)\b(sshpass\s+-p)\s+\S+"), r"\1 ***"),
    (re.compile(r"(?i)(\s-u\s+)\S+:\S+"), r"\1***:***"),
)

def redact(cmd: str) -> str:
    out = cmd
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        out = pattern.sub(replacement, out)
    return out

def record(root: str, kind: str, path: str, *, tool: str = "",
           cmd: str = "", snapshot: str = "", trash: str = "",
           task_id: str = "", manager: str = "", packages=None) -> None:
    entry: dict = {"ts": datetime.datetime.now().isoformat(timespec="microseconds"),
             "kind": kind, "path": path}
    if tool:
        entry["tool"] = tool
    if task_id:
        entry["task"] = task_id[:64]
    if cmd:
        entry["cmd"] = redact(cmd)[:400]
    if snapshot:
        entry["snapshot"] = snapshot
    if trash:
        entry["trash"] = trash
    if manager:
        entry["manager"] = manager
    if packages:
        entry["packages"] = list(packages)
    try:
        os.makedirs(root, exist_ok=True)
        with open(journal_path(root), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(root)
    except OSError:
        pass

def _trim(root: str) -> None:
    path = journal_path(root)
    try:
        if os.path.getsize(path) <= JOURNAL_MAX_BYTES:
            return
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= JOURNAL_KEEP:
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-JOURNAL_KEEP:])
        os.replace(tmp, path)
    except OSError:
        pass

def read(root: str, limit: int = 50, since: str = "",
         task: str = "") -> list:
    path = journal_path(root)
    if not os.path.exists(path):
        return []
    entries = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if since and str(entry.get("ts", "")) < since:
                    continue
                if task and str(entry.get("task", "")) != task:
                    continue
                entries.append(entry)
    except OSError:
        return []
    entries.reverse()
    return entries[:limit] if limit else entries

def revertable(entry: dict, root: str = "") -> bool:
    kind = entry.get("kind")
    if kind in PACKAGE_KINDS:
        return bool(undo_command(entry)) and entry.get("ts") not in undone(root or _default_root())
    if kind == CREATE:
        return True
    if kind == MODIFY:
        return bool(entry.get("snapshot")) and os.path.exists(entry["snapshot"])
    if kind == DELETE:
        return bool(entry.get("trash")) and os.path.exists(entry["trash"])
    return False

def revert(entries: list, snapshot_before=None, root: str = "", runner=None) -> list:
    done = []
    for entry in entries:
        path = entry.get("path")
        kind = entry.get("kind")
        if not path:
            continue
        if kind in PACKAGE_KINDS:
            undo = undo_command(entry)
            if not undo:
                done.append((path, "nothing to undo"))
                continue
            ok, out = (runner or _run_undo)(undo)
            if ok:
                mark_undone(root or _default_root(), entry.get("ts", ""))
                done.append((path, "uninstalled" if kind == PKG_INSTALL else "reinstalled"))
            else:
                done.append((path, f"failed: {out[-200:]}"))
            continue
        try:
            if kind == MODIFY and entry.get("snapshot") \
                    and os.path.exists(entry["snapshot"]):
                if snapshot_before:
                    snapshot_before(path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(entry["snapshot"], path)
                done.append((path, "restored"))
            elif kind == DELETE and entry.get("trash") \
                    and os.path.exists(entry["trash"]):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.move(entry["trash"], path)
                done.append((path, "restored"))
            elif kind == CREATE and os.path.isfile(path):
                os.remove(path)
                done.append((path, "removed"))
            else:
                done.append((path, "nothing to undo"))
        except OSError as error:
            done.append((path, f"failed: {error}"))
    return done

def summarise(entries: list) -> str:
    if not entries:
        return "Nothing has changed yet."
    lines = []
    for entry in entries:
        when = str(entry.get("ts", ""))[:19].replace("T", " ")
        tool = entry.get("tool") or "?"
        note = "" if revertable(entry) else "  [no longer revertable]"
        lines.append(f"{when}  {entry.get('kind','?'):6}  "
                     f"{entry.get('path','?')}  ({tool}){note}")
        if entry.get("cmd"):
            lines.append(f"          via: {entry['cmd']}")
    return "\n".join(lines)


def _run_undo(cmd: str):
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return r.returncode == 0, (r.stdout + r.stderr).strip()
