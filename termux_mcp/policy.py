import json
import os
import re
import threading

from . import safety

DEFAULT = {"deny_paths": [], "allow_paths": [], "deny_tools": [], "network": True,
           "pip": "any", "sandbox_unknown_scripts": False}
KEYS = tuple(DEFAULT)
_lock = threading.Lock()
_cache = {"mtime": None, "path": "", "value": dict(DEFAULT)}

_NET = re.compile(
    r"(?:^|[\s;&|(])(?:curl|wget|aria2c|ssh|scp|sftp|rsync\s+\S*\S+:|nc|ncat|telnet|ftp|ping|"
    r"cloudflared|ngrok|yt-dlp|"
    r"(?:pkg|apt|apt-get)\s+(?:install|update|upgrade|reinstall)|"
    r"pip3?\s+(?:install|download)|python3?\s+-m\s+pip\s+(?:install|download)|"
    r"npm\s+(?:install|i|ci|update)|yarn(?:\s+(?:add|install))?|pnpm\s+(?:add|install)|"
    r"git\s+(?:clone|pull|fetch|push)|gem\s+install|cargo\s+install|go\s+(?:get|install))\b")
_PIP_INSTALL = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?(?:pip3?|python3?\s+-m\s+pip)\s+install\b")
_IN_VENV = re.compile(r"(?:venv|\.venv|env|virtualenv)/bin/(?:pip3?|python3?)\b|\bsource\s+\S*activate\b|"
                      r"\.\s+\S*/activate\b|\bpip3?\s+install\s+.*--user\b")
_PATH_TOKEN = re.compile(r"(?:(?<=\s)|(?<=^)|(?<==)|(?<=[\"']))((?:~|\$HOME|/|\.{1,2}/)[^\s;&|<>\"'`()]*)")


def path() -> str:
    from .auth import config_dir
    return os.path.join(config_dir(), "policy.json")


def get() -> dict:
    p = path()
    with _lock:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = None
        if mtime is not None and _cache["path"] == p and _cache["mtime"] == mtime:
            return dict(_cache["value"])
        value = dict(DEFAULT)
        if mtime is not None:
            try:
                with open(p, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    value.update({k: data[k] for k in KEYS if k in data})
            except (OSError, ValueError):
                pass
        _cache.update(mtime=mtime, path=p, value=value)
        return dict(value)


def validate(data: dict) -> list:
    problems = []
    for key in data:
        if key not in KEYS:
            problems.append(f"unknown key {key}")
    for key in ("deny_paths", "allow_paths", "deny_tools"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(x, str) for x in data[key])):
            problems.append(f"{key} must be a list of strings")
    if "network" in data and not isinstance(data["network"], bool):
        problems.append("network must be true or false")
    if "pip" in data and data["pip"] not in ("any", "venv_only"):
        problems.append("pip must be 'any' or 'venv_only'")
    if "sandbox_unknown_scripts" in data and not isinstance(data["sandbox_unknown_scripts"], bool):
        problems.append("sandbox_unknown_scripts must be true or false")
    return problems


def set_policy(changes: dict, replace: bool = False) -> dict:
    if not isinstance(changes, dict):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "policy must be an object"}]}
    problems = validate(changes)
    if problems:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "; ".join(problems)}]}
    value = dict(DEFAULT) if replace else get()
    value.update(changes)
    p = path()
    tmp = p + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
        os.replace(tmp, p)
        _cache.update(mtime=None, path="")
    return {"ok": True, "policy": value, "summary": "policy saved"}


def _expand(p: str) -> str:
    p = p.strip().strip("'\"")
    if p.startswith("$HOME"):
        p = safety.HOME + p[5:]
    p = os.path.expanduser(p) if not p.startswith("~") else os.path.join(safety.HOME, p[1:].lstrip("/"))
    return os.path.normpath(p)


def _under(p: str, roots) -> bool:
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def _resolve(token: str, cwd: str) -> str:
    token = token.strip().strip("'\"")
    if token.startswith(("~", "$HOME")):
        return _expand(token)
    if not os.path.isabs(token):
        token = os.path.join(cwd or os.getcwd(), token)
    return os.path.normpath(token)


def _write_roots(pol: dict):
    roots = [_expand(p) for p in pol.get("allow_paths") or []]
    if not roots:
        return None
    extra = [safety.safety_root(""), os.path.join(safety.HOME, ".termux-mcp")]
    if os.environ.get("TMPDIR"):
        extra.append(os.path.normpath(os.environ["TMPDIR"]))
    return roots + extra


def check_tool(name: str) -> str:
    pol = get()
    if name in (pol.get("deny_tools") or []):
        return f"policy: the tool {name} is disabled on this device"
    return ""


def check_path(p: str, write: bool = False, cwd: str = "") -> str:
    if not p:
        return ""
    pol = get()
    real = _resolve(str(p), cwd)
    denied = [_expand(x) for x in pol.get("deny_paths") or []]
    if denied and _under(real, denied):
        return f"policy: {p} is in a protected folder"
    if write:
        roots = _write_roots(pol)
        if roots and not _under(real, roots):
            return f"policy: writes are only allowed under {', '.join(pol['allow_paths'])}"
    return ""


def check_command(cmd: str, tool: str = "run", cwd: str = "") -> str:
    pol = get()
    reason = check_tool(tool)
    if reason:
        return reason
    text = str(cmd or "")
    if pol.get("network") is False and _NET.search(text):
        return "policy: network access is off for this device"
    if pol.get("pip") == "venv_only" and _PIP_INSTALL.search(text) and not _IN_VENV.search(text) \
            and not os.environ.get("VIRTUAL_ENV"):
        return "policy: pip install is only allowed inside a venv (use venv_ensure)"
    denied = [_expand(x) for x in pol.get("deny_paths") or []]
    if denied:
        for token in _PATH_TOKEN.findall(text):
            if _under(_resolve(token, cwd), denied):
                return f"policy: {token} is in a protected folder"
    roots = _write_roots(pol)
    if roots:
        for target in safety.write_targets(text, include_removals=True):
            if not _under(target, roots):
                return f"policy: writes are only allowed under {', '.join(pol['allow_paths'])}"
    return ""


def summary() -> dict:
    pol = get()
    active = {k: v for k, v in pol.items() if v != DEFAULT[k]}
    return {"ok": True, "policy": pol, "path": path(), "active": active,
            "summary": ("custom policy: " + ", ".join(sorted(active))) if active else "default policy (no limits)"}
