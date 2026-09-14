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


def require_number(value, *, minimum=None, maximum=None) -> str:
    """Validate that a parameter is numeric and return it for interpolation.

    Numeric-looking parameters are interpolated straight into shell strings in
    many places (``-n {limit}``, ``-crf {crf}``, ``--id {nid}``), so this is
    the gate for them. Anything that is not actually a number raises rather
    than being passed through: a value like ``1; touch /tmp/x`` is a string,
    not a number, and must never reach the shell.

    Raises ValueError, which handlers surface as a 400.
    """
    if isinstance(value, bool):
        # bool is an int subclass; "True" is never a valid numeric argument.
        raise ValueError(f"Expected a number, got {value!r}")

    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number, got {value!r}") from exc

    # float("nan") and float("inf") parse happily and would produce a
    # nonsensical but injectable-looking argument.
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
    """Like require_number, but rejects fractional values.

    For parameters that are structurally integers — pids, limits, camera ids,
    signal numbers.
    """
    out = require_number(value, minimum=minimum, maximum=maximum)
    if "." in out or "e" in out.lower():
        raise ValueError(f"Expected a whole number, got {value!r}")
    return out


# Legacy name. It never validated — it coerced, and fell back to quoting — so
# numeric-looking parameters that were not numeric slipped through to the
# shell. Now it validates. Kept so existing call sites gain the check without
# every one of them being rewritten in the same change.
shell_quote_num = require_number


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
