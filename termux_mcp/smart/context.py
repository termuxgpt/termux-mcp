import hashlib
import json
import os
import platform

from . import base, cache

TOOLS = ("python", "pip", "node", "npm", "git", "clang", "make", "go", "rustc",
         "java", "ruby", "php", "tmux", "curl", "wget", "ssh", "ffmpeg", "sqlite3",
         "termux-battery-status", "tesseract", "zbarimg")


def _memory(path: str, limit: int = 8) -> list:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [l.strip("-* \t\n") for l in handle if l.strip() and not l.startswith("#")]
        return [l[:80] for l in lines[:limit]]
    except OSError:
        return []


def pack(etag: str = "", memory_file: str = "", cwd: str = "") -> dict:
    cached = cache.get("context", memory_file)
    if cached is None:
        home = base.home()
        present = [t for t in TOOLS if base.which(t)]
        api = "termux-battery-status" in present
        storage = os.path.isdir(os.path.join(home, "storage", "shared"))
        from .fixgraph import recent_failures
        body = {
            "home": home,
            "cwd": cwd or os.getcwd(),
            "arch": platform.machine(),
            "android": os.environ.get("TERMUX_VERSION", "") and
                       base.sh("getprop ro.build.version.release", timeout=3,
                               check_risk=False).out.strip(),
            "termux": os.environ.get("TERMUX_VERSION", ""),
            "tools": [t for t in present if not t.startswith("termux-")],
            "termux_api": api,
            "storage": storage,
        }
        mem = _memory(memory_file or os.path.join(home, ".termuxgpt_memory.md"))
        if mem:
            body["memory"] = mem
        fails = recent_failures(3)
        if fails:
            body["recent_failures"] = fails
        cache.put("context", body, memory_file)
        cached = body
    tag = hashlib.sha1(json.dumps(cached, sort_keys=True).encode()).hexdigest()[:12]
    if etag and etag == tag:
        return {"ok": True, "unchanged": True, "etag": tag}
    out = dict(cached)
    out.update(ok=True, etag=tag)
    return out
