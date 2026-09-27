import base64
import os
import re
import shlex
import tempfile


def shell_quote(s: str) -> str:
    if not s:
        return "''"
    try:
        return shlex.quote(s)
    except Exception:
        quoted = s.replace("'", "'\\''")
        return f"'{quoted}'"


def require_number(value, *, minimum=None, maximum=None) -> str:
    if isinstance(value, bool):
        raise ValueError(f"Expected a number, got {value!r}")

    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc

    if num != num or num in (float("inf"), float("-inf")):
        raise ValueError(f"Expected a finite number, got {value!r}")

    if minimum is not None and num < minimum:
        raise ValueError(f"Value {num} is below the minimum of {minimum}")
    if maximum is not None and num > maximum:
        raise ValueError(f"Value {num} is above the maximum of {maximum}")

    if num == int(num):
        return str(int(num))
    return repr(num)


def require_int(value, *, minimum=None, maximum=None) -> str:
    out = require_number(value, minimum=minimum, maximum=maximum)
    if "." in out or "e" in out.lower():
        raise ValueError(f"Expected a whole number, got {value!r}")
    return out


shell_quote_num = require_number


def json_response(handler, status: int, data: dict) -> None:
    import json
    body = json.dumps(data).encode("utf-8")
    try:
        handler._last_status = status
        handler._last_payload = data
    except AttributeError:
        pass
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def is_safe_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    try:
        expanded = os.path.expanduser(path)
        real = os.path.realpath(expanded)
        real_unix = real.replace('\\', '/')
    except (ValueError, OSError):
        return False
    import posixpath
    norm = posixpath.normpath(expanded.replace('\\', '/'))
    blocked = ('/dev/', '/proc/', '/sys/')
    for prefix in blocked:
        if real_unix.startswith(prefix) or norm.startswith(prefix):
            return False
    return True


def tmp_dir() -> str:
    candidates = [
        os.environ.get("TMPDIR", ""),
        os.path.join(
            os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "tmp"
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def split_cd_chain(rest: str) -> tuple:
    cut_at = None
    cut_sep = None
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1 and (cut_at is None or idx < cut_at):
            cut_at, cut_sep = idx, sep

    if cut_at is None:
        return rest.strip(), None

    assert cut_sep is not None
    return rest[:cut_at].strip(), rest[cut_at + len(cut_sep):].strip()


def expand_home(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return os.path.join(home, path[2:])
    return path


def kill_process_group(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
    except Exception:
        return

    import signal

    try:
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
    except Exception:
        pass

    try:
        process.kill()
    except Exception:
        pass


_SENSITIVE_HOME_PREFIXES = (
    ".ssh/",
    ".termux/",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".config/fish/",
)


_SENSITIVE_PREFIX_PARTS = ("bin", "etc", "libexec")


def is_sensitive_path(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False

    try:
        real = os.path.realpath(os.path.expanduser(path)).replace("\\", "/")
    except (ValueError, OSError):
        return True

    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if real == home or real.startswith(home + "/"):
        rel = real[len(home):].lstrip("/")
        if rel.startswith(_SENSITIVE_HOME_PREFIXES):
            return True

    prefix = os.environ.get(
        "PREFIX", "/data/data/com.termux/files/usr"
    ).replace("\\", "/").rstrip("/")
    for sub in _SENSITIVE_PREFIX_PARTS:
        if real == f"{prefix}/{sub}" or real.startswith(f"{prefix}/{sub}/"):
            return True

    return False


def is_install_command(cmd: str) -> bool:
    return bool(re.search(
        r'\b(pkg|apt|apt-get)\s+(install|upgrade|dist-upgrade)\b', cmd
    ))


def encode_base64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")
