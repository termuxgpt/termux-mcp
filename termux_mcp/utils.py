import base64
import os
import re
import shlex


def shell_quote(s: str) -> str:
    if not s:
        return "''"
    try:
        return shlex.quote(s)
    except Exception:
        quoted = s.replace("'", "'\\''")
        return f"'{quoted}'"


def shell_quote_num(value) -> str:
    try:
        num = float(value)
        if num == int(num):
            return str(int(num))
        return str(num)
    except (ValueError, TypeError):
        return shell_quote(str(value))


def json_response(handler, status: int, data: dict) -> None:
    import json
    body = json.dumps(data).encode("utf-8")
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


def is_install_command(cmd: str) -> bool:
    return bool(re.search(
        r'\b(pkg|apt|apt-get)\s+(install|upgrade|dist-upgrade)\b', cmd
    ))


def encode_base64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")
