import datetime
import glob
import hashlib
import os
import re
import shutil
from typing import List, Optional

from . import changes
from .config import HOME
from .shell import get_current_dir

SNAPSHOT_KEEP = 20
SNAPSHOT_KEEP_HOURS = 24
SNAPSHOT_KEEP_MAX = 200

_DEV_NULLISH = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def safety_root(*parts: str) -> str:
    return os.path.join(HOME, "termuxGPT", *parts)


def inside_safety_area(path: str) -> bool:
    try:
        real = os.path.realpath(path).replace("\\", "/")
        root = os.path.realpath(safety_root("")).replace("\\", "/").rstrip("/")
    except (OSError, ValueError):
        return True
    return real == root or real.startswith(root + "/")


def prune_old_dirs(root: str, keep: int) -> None:
    dirs = sorted(glob.glob(os.path.join(root, "*")))
    cutoff = datetime.datetime.now() - datetime.timedelta(
        hours=SNAPSHOT_KEEP_HOURS)
    alive = set(dirs[-keep:])
    for path in dirs:
        try:
            made = datetime.datetime.strptime(os.path.basename(path),
                                              "%Y%m%d-%H%M%S-%f")
        except ValueError:
            continue
        if made >= cutoff:
            alive.add(path)
    kept = sorted(alive)
    doomed = [d for d in dirs if d not in alive]
    if len(kept) > SNAPSHOT_KEEP_MAX:
        doomed += kept[:len(kept) - SNAPSHOT_KEEP_MAX]
    for stale in doomed:
        try:
            shutil.rmtree(stale)
        except OSError:
            pass


def snapshot_before_write(path: str, tool: str = "", cmd: str = "",
                          task_id: str = "") -> Optional[str]:
    root = safety_root("")
    if inside_safety_area(path):
        return None
    if not os.path.exists(path):
        changes.record(root, changes.CREATE, path, tool=tool, cmd=cmd,
                       task_id=task_id)
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if path.startswith(HOME):
        rel = os.path.relpath(path, HOME)
    else:
        rel = f"{os.path.basename(path)}.{hashlib.md5(path.encode()).hexdigest()[:8]}"
    snap = os.path.join(safety_root("snapshots"), ts, rel)
    try:
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        shutil.copy2(path, snap)
        prune_old_dirs(safety_root("snapshots"), SNAPSHOT_KEEP)
        changes.record(root, changes.MODIFY, path, tool=tool, cmd=cmd,
                       snapshot=snap, task_id=task_id)
        return snap
    except OSError:
        return None


def trash_path(path: str, tool: str = "", cmd: str = "",
               task_id: str = "") -> Optional[str]:
    root = safety_root("")
    if not os.path.exists(path):
        return None
    if inside_safety_area(path):
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trash = os.path.join(root, "trash", ts)
    try:
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, os.path.basename(path))
        shutil.move(path, dest)
        prune_old_dirs(os.path.join(root, "trash"), SNAPSHOT_KEEP)
        changes.record(root, changes.DELETE, path, tool=tool, cmd=cmd,
                       trash=dest, task_id=task_id)
        return dest
    except OSError:
        return None


def _expand_shell_path(token: str, cwd: Optional[str] = None) -> Optional[str]:
    if not token:
        return None
    token = token.strip().strip('"\'')
    if not token:
        return None
    if token.startswith("$HOME"):
        token = HOME + token[len("$HOME"):]
    elif token.startswith("~"):
        token = os.path.join(HOME, token[1:].lstrip("/"))
    if not os.path.isabs(token):
        token = os.path.join(cwd or get_current_dir(), token)
    return os.path.normpath(token)


_WRITE_PATTERNS = [
    re.compile(r"(?<!\S)(?:[12]?>>?|&>)\s*([^\s;&|]+)"),
    re.compile(r"\btee\s+(?:-[a-zA-Z]+\s+)*([^\s;&|]+)"),
    re.compile(r"\b(?:cp|mv)\s+(.*?)(?:[;&|]|$)"),
    re.compile(r"\bsed\s+(.*?)(?:[;&|]|$)"),
    re.compile(r"\btruncate\s+-s\s+\S+\s+([^\s;&|]+)"),
    re.compile(r"\bdd\s+(.*?)(?:[;&|]|$)"),
]


def _is_black_hole(path: str) -> bool:
    return (
        path in _DEV_NULLISH
        or path.startswith(("/dev/", "/proc/", "/sys/"))
        or inside_safety_area(path)
    )


_REMOVE_PATTERN = re.compile(
    r"\b(?:rm|rmdir|unlink|shred|mkdir|touch|chmod|chown|ln)\s+(.*?)(?:[;&|]|$)")


def write_targets(cmd: str, include_removals: bool = False) -> List[str]:
    targets = set()
    cwd = get_current_dir()
    m = re.match(r"\s*cd\s+([^\s;&|]+)", cmd)
    if m:
        base = _expand_shell_path(m.group(1), cwd)
        if base and os.path.isdir(base):
            cwd = base

    def add(token: str, must_exist_parent: bool = True) -> None:
        if not token or re.search(r"[&|;<>`]", token):
            return
        if "$" in token and not token.startswith("$HOME"):
            return
        path = _expand_shell_path(token, cwd)
        if not path or _is_black_hole(path):
            return
        if not must_exist_parent:
            targets.add(path)
        elif os.path.isfile(path):
            targets.add(path)
        elif os.path.isdir(os.path.dirname(path) or "."):
            targets.add(path)

    for m in _WRITE_PATTERNS[0].finditer(cmd):
        add(m.group(1))
    for m in _WRITE_PATTERNS[1].finditer(cmd):
        add(m.group(1))
    for m in _WRITE_PATTERNS[2].finditer(cmd):
        args = m.group(1).strip()
        if args:
            add(args.split()[-1])
    for m in _WRITE_PATTERNS[3].finditer(cmd):
        args = m.group(1).strip()
        if re.search(r"(?:^|\s)(?:-i|--in-place)(?:\s|$)", args) and args:
            add(args.split()[-1])
    for m in _WRITE_PATTERNS[4].finditer(cmd):
        add(m.group(1))
    for m in _WRITE_PATTERNS[5].finditer(cmd):
        for tok in m.group(1).split():
            if tok.startswith("of="):
                add(tok[3:])
    if include_removals:
        for m in _REMOVE_PATTERN.finditer(cmd):
            for tok in m.group(1).split():
                if not tok.startswith("-"):
                    add(tok, must_exist_parent=False)
    return sorted(targets)


def snapshot_targets_from_command(cmd: str, task_id: str = "") -> List[str]:
    snaps = []
    for path in write_targets(cmd):
        snap = snapshot_before_write(path, tool="run", cmd=cmd,
                                         task_id=task_id)
        if snap:
            snaps.append(snap)
    return snaps
