import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional

from . import safety

TOKEN_FILE = "token"
PAIR_FILE = "pair.json"
CAPS_FILE = "caps.json"
PAIR_TTL = 300
PAIR_TRIES = 5
PAIR_RATE_WINDOW = 60
PAIR_RATE_MAX = 10
CAP_PREFIX = "cap_"

_lock = threading.Lock()
_token_cache = {"path": "", "mtime": None, "value": ""}
_pair_hits = {}


def config_dir() -> str:
    path = os.environ.get("TERMUX_MCP_CONFIG_DIR") or os.path.join(safety.HOME, ".termux-mcp")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _path(name: str) -> str:
    return os.path.join(config_dir(), name)


def disabled() -> bool:
    return os.environ.get("TERMUX_MCP_AUTH", "").strip().lower() in ("off", "0", "false", "no", "none")


def _write_private(path: str, text: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def master_token() -> str:
    env = os.environ.get("TERMUX_MCP_AUTH_TOKEN", "")
    if env:
        return env
    if disabled():
        return ""
    path = _path(TOKEN_FILE)
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if mtime is not None and _token_cache["path"] == path and _token_cache["mtime"] == mtime:
            return _token_cache["value"]
        value = ""
        if mtime is not None:
            try:
                with open(path, encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError:
                value = ""
        if len(value) < 16:
            value = _new_token()
            _write_private(path, value + "\n")
            mtime = os.path.getmtime(path)
        _token_cache.update(path=path, mtime=mtime, value=value)
        return value


def required() -> bool:
    return bool(master_token())


def rotate() -> str:
    if os.environ.get("TERMUX_MCP_AUTH_TOKEN"):
        raise RuntimeError("TERMUX_MCP_AUTH_TOKEN is set in the environment; change it there")
    value = _new_token()
    with _lock:
        _write_private(_path(TOKEN_FILE), value + "\n")
        _token_cache.update(path="", mtime=None, value="")
    return value


class Principal:
    __slots__ = ("id", "label", "tools", "paths", "read_only", "expires")

    def __init__(self, cid, label="", tools=None, paths=None, read_only=False, expires=0.0):
        self.id = cid
        self.label = label
        self.tools = set(tools) if tools else None
        self.paths = [os.path.normpath(os.path.expanduser(p)) for p in (paths or [])] or None
        self.read_only = bool(read_only)
        self.expires = float(expires or 0)

    def expired(self) -> bool:
        return bool(self.expires) and time.time() > self.expires

    def allows_tool(self, name: str) -> bool:
        if self.tools is None:
            return True
        return name in self.tools

    def allows_path(self, path: str) -> bool:
        if self.paths is None or not path:
            return True
        real = os.path.normpath(os.path.expanduser(str(path)))
        if not os.path.isabs(real):
            return True
        return any(real == p or real.startswith(p.rstrip("/") + "/") for p in self.paths)

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "tools": sorted(self.tools) if self.tools else "all",
                "paths": self.paths or "all", "read_only": self.read_only,
                "expires": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.expires)) if self.expires else "never"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_caps() -> dict:
    try:
        with open(_path(CAPS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_caps(caps: dict) -> None:
    _write_private(_path(CAPS_FILE), json.dumps(caps, indent=1))


def cap_issue(tools=None, paths=None, read_only=False, ttl_min=60, label="") -> dict:
    try:
        ttl = max(1, min(int(ttl_min or 60), 60 * 24 * 30))
    except (TypeError, ValueError):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "ttl_min must be a number of minutes"}]}
    if tools is not None and not isinstance(tools, list):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "tools must be a list"}]}
    if paths is not None and not isinstance(paths, list):
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "paths must be a list"}]}
    token = CAP_PREFIX + secrets.token_urlsafe(24)
    cid = "c-" + secrets.token_hex(3)
    expires = time.time() + ttl * 60
    entry = {"id": cid, "label": str(label)[:60], "tools": [str(t) for t in (tools or [])],
             "paths": [str(p) for p in (paths or [])], "read_only": bool(read_only),
             "expires": expires, "created": time.time()}
    with _lock:
        caps = _load_caps()
        caps = {h: c for h, c in caps.items() if c.get("expires", 0) > time.time()}
        caps[_hash(token)] = entry
        _save_caps(caps)
    p = Principal(cid, entry["label"], entry["tools"], entry["paths"], entry["read_only"], expires)
    return {"ok": True, "token": token, "id": cid, "scope": p.describe(),
            "summary": f"capability {cid} valid for {ttl} min — the token is shown only once"}


def cap_list() -> dict:
    now = time.time()
    rows = []
    for entry in _load_caps().values():
        p = Principal(entry["id"], entry.get("label", ""), entry.get("tools"), entry.get("paths"),
                      entry.get("read_only"), entry.get("expires"))
        row = p.describe()
        row["active"] = entry.get("expires", 0) > now
        rows.append(row)
    return {"ok": True, "caps": rows, "summary": f"{sum(r['active'] for r in rows)} active capability token(s)"}


def cap_revoke(cid: str) -> dict:
    with _lock:
        caps = _load_caps()
        keep = {h: c for h, c in caps.items() if c.get("id") != cid and cid != "all"}
        removed = len(caps) - len(keep)
        _save_caps(keep)
    return {"ok": True, "revoked": removed, "summary": f"revoked {removed} token(s)"}


def _principal_for_cap(token: str) -> Optional[Principal]:
    entry = _load_caps().get(_hash(token))
    if not entry:
        return None
    p = Principal(entry["id"], entry.get("label", ""), entry.get("tools"), entry.get("paths"),
                  entry.get("read_only"), entry.get("expires"))
    return None if p.expired() else p


def authenticate(token: str):
    expected = master_token()
    if not expected:
        return True, None
    token = (token or "").strip()
    if not token:
        return False, None
    if hmac.compare_digest(token.encode(), expected.encode()):
        return True, None
    if token.startswith(CAP_PREFIX):
        p = _principal_for_cap(token)
        if p is not None:
            return True, p
    return False, None


def bearer(header_value: str) -> str:
    value = (header_value or "").strip()
    if value[:7].lower() == "bearer ":
        return value[7:].strip()
    return ""


def pair_start(ttl: int = PAIR_TTL) -> dict:
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = {"hash": _hash(code), "expires": time.time() + ttl, "tries": 0}
    with _lock:
        _write_private(_path(PAIR_FILE), json.dumps(record))
    return {"code": code, "expires_in": ttl}


def _rate_limited(client: str) -> bool:
    now = time.time()
    hits = [t for t in _pair_hits.get(client, []) if now - t < PAIR_RATE_WINDOW]
    hits.append(now)
    _pair_hits[client] = hits
    return len(hits) > PAIR_RATE_MAX


def pair_claim(code: str, client: str = "") -> dict:
    if _rate_limited(client or "?"):
        return {"ok": False, "status": 429, "error": "Too many attempts; wait a minute"}
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    path = _path(PAIR_FILE)
    with _lock:
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return {"ok": False, "status": 403, "error": "No pairing in progress: run `termux-mcp pair` in Termux"}
        if record.get("expires", 0) < time.time():
            _remove(path)
            return {"ok": False, "status": 403, "error": "Pairing code expired: run `termux-mcp pair` again"}
        if len(code) != 6 or not hmac.compare_digest(_hash(code), record.get("hash", "")):
            record["tries"] = int(record.get("tries", 0)) + 1
            if record["tries"] >= PAIR_TRIES:
                _remove(path)
                return {"ok": False, "status": 403, "error": "Too many wrong codes: run `termux-mcp pair` again"}
            _write_private(path, json.dumps(record))
            return {"ok": False, "status": 403, "error": "Wrong code",
                    "tries_left": PAIR_TRIES - record["tries"]}
        _remove(path)
    token = master_token()
    if not token:
        return {"ok": True, "status": 200, "token": "", "auth": "off"}
    return {"ok": True, "status": 200, "token": token}


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def cli(argv) -> int:
    import shutil
    import subprocess
    from .config import HOST, PORT

    cmd = argv[0] if argv else ""
    if cmd == "token":
        if "--rotate" in argv:
            try:
                value = rotate()
            except RuntimeError as error:
                print(error)
                return 1
            print("New token (paired apps must pair again):")
            print(value)
            return 0
        value = master_token()
        print(value if value else "Authentication is off (TERMUX_MCP_AUTH=off).")
        return 0
    if cmd == "pair":
        if not master_token():
            print("Authentication is off (TERMUX_MCP_AUTH=off): nothing to pair.")
            return 0
        info = pair_start()
        link = f"termuxgpt://pair?host={HOST}&port={PORT}&code={info['code']}"
        print("\n  Pairing code:  " + " ".join(info["code"][:3]) + "  " + " ".join(info["code"][3:]))
        print(f"  Valid for {info['expires_in'] // 60} minutes. Enter it in the app when it asks.\n")
        if shutil.which("qrencode"):
            try:
                subprocess.run(["qrencode", "-t", "ANSIUTF8", link], timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        print("  " + link + "\n")
        return 0
    print("usage: termux-mcp [pair | token [--rotate]]")
    return 2
