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


def tmp_dir() -> str:
    """A directory this process can actually write temporary files to.

    Termux has $TMPDIR ($PREFIX/tmp) and that works. It also has /tmp — the
    Android system one, owned by `shell` with mode 0771 — which this process
    can traverse but NOT write to. Handlers that hardcoded /tmp therefore
    failed with "Permission denied": /patch could never apply a diff, and
    migrate could neither back up nor restore.
    """
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


def kill_process_group(process) -> None:
    """Terminate a spawned child and anything it started, if still running.

    Commands are spawned with ``preexec_fn=os.setsid``, so the child is a
    session leader and its process group id equals its pid — killing the
    group takes down its descendants too.

    This exists because nothing used to kill the child at all. All three
    executors cleared their bookkeeping in ``finally`` and left the process
    running, and because COMMAND_TIMEOUT defaults to 0 the watchdog never
    armed. A client that disconnected mid-command, or any exception in the
    streaming loop, therefore left a process running forever — untracked and
    uncancellable.

    Safe to call on the normal path: an already-reaped process is a no-op.
    """
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


# Paths under $HOME that grant persistence or credential access when written.
# Writing here is not refused — editing files is what this server is for — but
# it requires an explicit confirmation. The difference these represent is
# between "changed a config" and "installed something that survives a reboot".
_SENSITIVE_HOME_PREFIXES = (
    ".ssh/",           # authorized_keys -> SSH into the device
    ".termux/",        # boot/start.sh -> runs at device boot
    ".bashrc",         # runs on every interactive shell
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".config/fish/",
)


# Directory names under $PREFIX that put code or configuration where it will be
# executed or trusted. Deliberately not all of $PREFIX — see is_sensitive_path.
_SENSITIVE_PREFIX_PARTS = ("bin", "etc", "libexec")


def is_sensitive_path(path: str) -> bool:
    """True if a path can be used to gain persistent access to the device.

    Also covers anything under $PREFIX (Termux's usr/), which holds the
    binaries and sshd configuration — writing there is installing software.
    """
    if not path or not isinstance(path, str):
        return False

    try:
        real = os.path.realpath(os.path.expanduser(path)).replace("\\", "/")
    except (ValueError, OSError):
        # Cannot resolve it, so cannot vouch for it.
        return True

    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if real == home or real.startswith(home + "/"):
        rel = real[len(home):].lstrip("/")
        if rel.startswith(_SENSITIVE_HOME_PREFIXES):
            return True

    # Only the parts of $PREFIX that hold code or configuration, NOT all of it.
    #
    # Testing on a device showed why: $TMPDIR is $PREFIX/tmp, so treating the
    # whole prefix as sensitive made writing an ordinary temporary file require
    # confirmation. bin/ is on PATH so anything there gets executed, etc/ holds
    # sshd_config, and libexec/ is the same idea.
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
