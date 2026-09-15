import datetime
import json
import os
import shutil

JOURNAL_KEEP = 2000
JOURNAL_MAX_BYTES = 400_000

CREATE = "create"
MODIFY = "modify"
DELETE = "delete"


def journal_path(root: str) -> str:
    return os.path.join(root, "changes.jsonl")


def record(root: str, kind: str, path: str, *, tool: str = "",
           cmd: str = "", snapshot: str = "", trash: str = "") -> None:
    entry = {"ts": datetime.datetime.now().isoformat(timespec="microseconds"),
             "kind": kind, "path": path}
    if tool:
        entry["tool"] = tool
    if cmd:
        entry["cmd"] = cmd[:400]
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


def read(root: str, limit: int = 50, since: str = "") -> list:
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
