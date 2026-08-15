"""File safety: snapshot before overwrite, trash instead of delete.

Every file mutation through this server is recoverable. Originals land in
~/termuxGPT/snapshots/ (mirrored from HOME; hashed basename outside HOME)
and deletions move into ~/termuxGPT/trash/ — nothing is ever destroyed.

`/write` and `/delete` snapshot their single target directly. `/run` shell
commands get a best-effort scan for write patterns (redirects, tee, sed -i,
cp/mv destinations, truncate, dd of=) so shell-based writes to real files
are protected too. Commands whose writes can't be parsed (git checkout,
tar/unzip extraction, python scripts writing files) remain the documented
gap — the client-side confirmation dialog still gates those.
"""

import datetime
import glob
import hashlib
import os
import re
import shutil
from typing import List, Optional

from .config import HOME
from .shell import get_current_dir

SNAPSHOT_KEEP = 20  # newest snapshot dirs to retain

# /dev/null and friends — redirects to these are not file writes.
_DEV_NULLISH = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def safety_root(*parts: str) -> str:
    return os.path.join(HOME, "termuxGPT", *parts)


def prune_old_dirs(root: str, keep: int) -> None:
    """Keep the `keep` newest timestamped dirs under `root`, drop the rest."""
    dirs = sorted(glob.glob(os.path.join(root, "*")))
    for stale in dirs[:-keep]:
        try:
            shutil.rmtree(stale)
        except OSError:
            pass


def snapshot_before_write(path: str) -> Optional[str]:
    """Copy `path` to ~/termuxGPT/snapshots/<ts>/<rel> before it is
    overwritten. Returns the snapshot path, or None if there was nothing
    to protect (new file, missing, or already inside termuxGPT/)."""
    if not os.path.exists(path):
        return None
    if path.startswith(safety_root("")):
        return None  # never snapshot our own safety folders
    # Microsecond ts: every write gets its own dir (second-resolution would
    # collapse rapid writes into one dir and defeat per-write pruning).
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
        return snap
    except OSError:
        return None


def trash_path(path: str) -> Optional[str]:
    """Move `path` into ~/termuxGPT/trash/<ts>/ instead of deleting it.
    Returns the trashed destination, or None on failure."""
    if not os.path.exists(path):
        return None
    if path.startswith(safety_root("")):
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trash = os.path.join(safety_root("trash"), ts)
    try:
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, os.path.basename(path))
        shutil.move(path, dest)
        prune_old_dirs(safety_root("trash"), SNAPSHOT_KEEP)
        return dest
    except OSError:
        return None


# ── Shell command scan (/run) ──────────────────────────────────────────────

def _expand_shell_path(token: str, cwd: Optional[str] = None) -> Optional[str]:
    """Resolve a shell-ish path token (~, $HOME, relative) to an absolute
    path. Returns None for tokens that aren't usable paths."""
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


# Each pattern group(1) is the file token that may be overwritten.
_WRITE_PATTERNS = [
    # Redirections: >, >>, 1>, 2>, &>  (echo hi > file, cmd >> log, ...)
    re.compile(r"(?<!\S)(?:[12]?>>?|&>)\s*([^\s;&|]+)"),
    # tee <file>
    re.compile(r"\btee\s+(?:-[a-zA-Z]+\s+)*([^\s;&|]+)"),
    # cp / mv — destination is the last bare token before ; & |
    re.compile(r"\b(?:cp|mv)\s+(.*?)(?:[;&|]|$)"),
    # sed -i / --in-place — file is the last bare token before ; & |
    re.compile(r"\bsed\s+(.*?)(?:[;&|]|$)"),
    # truncate -s N <file>
    re.compile(r"\btruncate\s+-s\s+\S+\s+([^\s;&|]+)"),
    # dd of=<file>
    re.compile(r"\bdd\s+(.*?)(?:[;&|]|$)"),
]


def _is_black_hole(path: str) -> bool:
    return (
        path in _DEV_NULLISH
        or path.startswith(("/dev/", "/proc/", "/sys/"))
        or path.startswith(safety_root(""))
    )


def snapshot_targets_from_command(cmd: str) -> List[str]:
    """Best-effort detection of files a shell command may overwrite.
    Returns the snapshot paths actually taken (existing regular files)."""
    targets = set()
    # Leading `cd dir && ...` changes the base for relative path tokens.
    cwd = get_current_dir()
    m = re.match(r"\s*cd\s+([^\s;&|]+)", cmd)
    if m:
        base = _expand_shell_path(m.group(1), cwd)
        if base and os.path.isdir(base):
            cwd = base

    def add(token: str) -> None:
        path = _expand_shell_path(token, cwd)
        if not path or _is_black_hole(path):
            return
        if os.path.isfile(path):
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

    snaps = []
    for path in sorted(targets):
        snap = snapshot_before_write(path)
        if snap:
            snaps.append(snap)
    return snaps
