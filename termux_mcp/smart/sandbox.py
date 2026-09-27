import difflib
import filecmp
import hashlib
import json
import os
import shutil
import tempfile

from .. import safety
from . import base, digest

MAX_BYTES = 60 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "build", "dist"}


def _size(path: str) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
            if total > MAX_BYTES:
                return total
    return total


def _ignore(_dir, names):
    return [n for n in names if n in SKIP_DIRS]


def _walk(root):
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            full = os.path.join(dirpath, f)
            files[os.path.relpath(full, root)] = full
    return files


def run(cmd: str, path: str = "", timeout: int = 120) -> dict:
    src = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if not os.path.isdir(src):
        return {"ok": False, "errors": [{"code": "E_NO_FILE", "msg": f"{src} is not a folder"}]}
    if _size(src) > MAX_BYTES:
        return {"ok": False, "errors": [{"code": "E_TOO_BIG", "msg": "folder over 60 MB; sandbox a subfolder"}]}
    sid = "sb-" + hashlib.sha1(f"{src}{cmd}{os.getpid()}{os.urandom(4).hex()}".encode()).hexdigest()[:8]
    box = os.path.join(base.ensure_dir(base.root("sandbox")), sid)
    work = os.path.join(box, "work")
    shutil.copytree(src, work, ignore=_ignore, symlinks=True)
    res = base.sh(cmd, timeout=timeout, cwd=work)
    d = digest.from_shell(res)
    before, after = _walk(src), _walk(work)
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after)
                      if not filecmp.cmp(before[p], after[p], shallow=False))
    diffs = {}
    for rel in modified[:5]:
        try:
            with open(before[rel], encoding="utf-8") as a, open(after[rel], encoding="utf-8") as b:
                diffs[rel] = "\n".join(list(difflib.unified_diff(a.read().splitlines(), b.read().splitlines(),
                                                                 lineterm="", n=1))[:30])
        except (OSError, UnicodeDecodeError):
            diffs[rel] = "(binary)"
    meta = {"src": src, "cmd": cmd, "added": added, "deleted": deleted, "modified": modified}
    with open(os.path.join(box, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    return {"ok": d["ok"], "sandbox": sid, "run": {k: v for k, v in d.items()
                                                  if k in ("ok", "exit", "summary", "errors", "out", "excerpt", "out_ref")},
            "added": added[:30], "modified": modified[:30], "deleted": deleted[:30], "diffs": diffs,
            "summary": f"would add {len(added)}, change {len(modified)}, delete {len(deleted)} file(s)"
                       f" — sandbox_apply {sid} to keep it"}


def apply(sandbox: str, confirmed: bool = False, task_id: str = "") -> dict:
    sid = "".join(c for c in str(sandbox) if c.isalnum() or c == "-")
    box = os.path.join(base.root("sandbox"), sid)
    try:
        with open(os.path.join(box, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return {"ok": False, "errors": [{"code": "E_NO_SANDBOX", "msg": f"no sandbox {sid}"}]}
    n = len(meta["added"]) + len(meta["modified"]) + len(meta["deleted"])
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "changes": n,
                "errors": [{"code": "E_CONFIRM", "msg": "resend with confirmed: true"}]}
    work, src = os.path.join(box, "work"), meta["src"]
    for rel in meta["added"] + meta["modified"]:
        dst = os.path.join(src, rel)
        safety.snapshot_before_write(dst, tool="sandbox_apply", task_id=task_id)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(work, rel), dst)
    for rel in meta["deleted"]:
        safety.trash_path(os.path.join(src, rel), tool="sandbox_apply", task_id=task_id)
    shutil.rmtree(box, ignore_errors=True)
    return {"ok": True, "applied": n, "summary": f"applied {n} change(s) to {src} (undo available)"}


def discard(sandbox: str) -> dict:
    sid = "".join(c for c in str(sandbox) if c.isalnum() or c == "-")
    shutil.rmtree(os.path.join(base.root("sandbox"), sid), ignore_errors=True)
    return {"ok": True, "summary": f"discarded {sid}"}
