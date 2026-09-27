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
CAPS_FILE = "caps.json"
CAP_PREFIX = "cap_"

_lock = threading.Lock()
_token_cache = {"path": "", "mtime": None, "value": ""}


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
    return os.environ.get("TERMUX_MCP_AUTH", "").strip().lower() not in ("on", "1", "true", "yes", "enabled")


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


def cli(argv) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "token":
        if "--rotate" in argv:
            try:
                value = rotate()
            except RuntimeError as error:
                print(error)
                return 1
            print("New token (clients must be updated):")
            print(value)
            return 0
        value = master_token()
        print(value if value else "Authentication is off (set TERMUX_MCP_AUTH=on to enable).")
        return 0
    if cmd == "pair":
        print("Pairing was removed. Authentication is off by default; set")
        print("TERMUX_MCP_AUTH=on to require a token, then use `termux-mcp token`.")
        return 0
    print("usage: termux-mcp [token [--rotate]]")
    return 2
