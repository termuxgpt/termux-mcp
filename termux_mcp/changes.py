import datetime
import json
import os
import re
import shutil

JOURNAL_KEEP = 2000
JOURNAL_MAX_BYTES = 400_000

CREATE = "create"
MODIFY = "modify"
DELETE = "delete"

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
           task_id: str = "") -> None:
    entry = {"ts": datetime.datetime.now().isoformat(timespec="microseconds"),
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

def revertable(entry: dict) -> bool:
    kind = entry.get("kind")
    if kind == CREATE:
        return True
    if kind == MODIFY:
        return bool(entry.get("snapshot")) and os.path.exists(entry["snapshot"])
    if kind == DELETE:
        return bool(entry.get("trash")) and os.path.exists(entry["trash"])
    return False

def revert(entries: list, snapshot_before=None) -> list:
    done = []
    for entry in entries:
        path = entry.get("path")
        kind = entry.get("kind")
        if not path:
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
