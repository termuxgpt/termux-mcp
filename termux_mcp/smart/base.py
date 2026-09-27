import hashlib
import json
import os
import subprocess
import threading
import time
from typing import Optional

from .. import safety

INLINE_CHARS = 1200
EXCERPT_HEAD = 6
EXCERPT_TAIL = 10
OUT_KEEP = 200
DEFAULT_TIMEOUT = 120
TERMUX_API_TIMEOUT = 25


def home() -> str:
    return safety.HOME


def root(*parts: str) -> str:
    path = safety.safety_root(*parts)
    return path


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def result(body: dict) -> dict:
    body.setdefault("ok", not body.get("errors"))
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return {"text": text, "is_error": not body.get("ok", False),
            "digest": body}


def fail(code: str, message: str, **extra) -> dict:
    body = {"ok": False, "errors": [{"code": code, "msg": message}]}
    body.update(extra)
    return result(body)


def _out_dir() -> str:
    return ensure_dir(root("out"))


def store_output(text: str) -> str:
    ref = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
    path = os.path.join(_out_dir(), ref + ".txt")
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        _prune_outputs()
    except OSError:
        return ""
    return ref


def load_output(ref: str) -> Optional[str]:
    ref = "".join(c for c in str(ref) if c.isalnum())[:40]
    path = os.path.join(_out_dir(), ref + ".txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _prune_outputs() -> None:
    folder = _out_dir()
    try:
        files = sorted((os.path.getmtime(os.path.join(folder, f)), f)
                       for f in os.listdir(folder))
    except OSError:
        return
    for _, name in files[:-OUT_KEEP]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


class ShellResult:
    __slots__ = ("cmd", "exit", "out", "err", "elapsed", "timed_out", "blocked")

    def __init__(self, cmd, exit_code, out, err, elapsed, timed_out=False,
                 blocked=""):
        self.cmd = cmd
        self.exit = exit_code
        self.out = out
        self.err = err
        self.elapsed = elapsed
        self.timed_out = timed_out
        self.blocked = blocked

    @property
    def ok(self) -> bool:
        return self.exit == 0 and not self.timed_out and not self.blocked

    @property
    def text(self) -> str:
        if self.err and self.out:
            return self.out.rstrip("\n") + "\n" + self.err
        return self.out or self.err


def _timeout_for(cmd: str, timeout: Optional[int]) -> int:
    from .. import kernel
    return kernel.timeout_for(cmd, timeout, default=DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT


def sh(cmd: str, timeout: Optional[int] = None, cwd: Optional[str] = None,
       confirmed: bool = False, check_risk: bool = True,
       stdin: Optional[str] = None) -> ShellResult:
    from .. import kernel
    if check_risk:
        decision = kernel.gate(cmd, confirmed=confirmed, tool="run_digest", cwd=cwd or "")
        if decision["status"] in ("blocked", "denied"):
            return ShellResult(cmd, 126, "", decision["message"], 0.0,
                               blocked="blocked" if decision["status"] == "blocked" else "denied")
        if decision["status"] == "confirm":
            return ShellResult(cmd, 126, "", decision["message"], 0.0,
                               blocked="confirm")
    limit = _timeout_for(cmd, timeout)
    start = time.time()
    kwargs = dict(shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                  cwd=cwd or None, text=True, errors="replace")
    kwargs.update(kernel.popen_kwargs())
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as error:
        return ShellResult(cmd, 127, "", str(error), 0.0)
    try:
        out, err = proc.communicate(input=stdin, timeout=limit)
        return ShellResult(cmd, proc.returncode, out or "", err or "",
                           time.time() - start)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError):
            out, err = "", ""
        return ShellResult(cmd, 124, out or "", err or "",
                           time.time() - start, timed_out=True)


def _kill_group(proc) -> None:
    from .. import kernel
    kernel.kill_group(proc)


def which(name: str) -> bool:
    from shutil import which as _which
    return _which(name) is not None


_state_lock = threading.Lock()


def load_state(name: str, default):
    path = os.path.join(ensure_dir(root("state")), name + ".json")
    with _state_lock:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default


def save_state(name: str, value) -> None:
    path = os.path.join(ensure_dir(root("state")), name + ".json")
    tmp = path + ".tmp"
    with _state_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=0)
            os.replace(tmp, path)
        except OSError:
            pass


def boolish(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")
