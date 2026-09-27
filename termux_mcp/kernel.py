import contextvars
import os
import re
import threading
from contextlib import contextmanager
from typing import Optional

from . import policy
from .security import get_risk_assessment

TERMUX_API_TIMEOUT = 25
LONG_TERMUX = ("termux-setup-storage", "termux-change-repo", "termux-reload-settings")

_principal = contextvars.ContextVar("termux_mcp_principal", default=None)

_MUTATING = re.compile(
    r"(?:^|[\s;&|(])(?:rm|rmdir|unlink|mv|cp|dd|tee|touch|mkdir|chmod|chown|ln|truncate|shred|"
    r"kill|pkill|killall|reboot|crontab|"
    r"sed\s+(?:\S+\s+)*-i|"
    r"(?:pkg|apt|apt-get)\s+(?:install|uninstall|remove|purge|upgrade|update|reinstall|autoremove)|"
    r"pip3?\s+(?:install|uninstall)|python3?\s+-m\s+pip\s+(?:install|uninstall)|"
    r"npm\s+(?:install|i|ci|uninstall|update|run|start)|"
    r"git\s+(?:commit|push|pull|reset|checkout|switch|merge|rebase|clean|stash|add|rm|mv|clone|restore)|"
    r"termux-(?:sms-send|telephony-call|wallpaper|clipboard-set|notification|torch|vibrate|"
    r"media-player|tts-speak|volume|brightness|job-scheduler|wake-lock)|"
    r"python3?\s+\S+|node\s+\S+|bash\s+\S+|sh\s+\S+|\./\S+)\b")


def current_principal():
    return _principal.get()


@contextmanager
def acting_as(principal):
    token = _principal.set(principal)
    try:
        yield principal
    finally:
        _principal.reset(token)


def run_as(principal, fn, *args, **kwargs):
    with acting_as(principal):
        return fn(*args, **kwargs)


def _command_timeout() -> int:
    from . import config
    return int(getattr(config, "COMMAND_TIMEOUT", 0) or 0)


def timeout_for(cmd: str, requested: Optional[int] = None, default: int = 0) -> int:
    try:
        if requested and int(requested) > 0:
            return int(requested)
    except (TypeError, ValueError):
        pass
    base = _command_timeout()
    words = str(cmd or "").strip().split()
    first = ""
    for w in words:
        if "=" in w.split("/")[0] and not w.startswith(("-", "/")):
            continue
        first = w
        break
    if first.startswith("termux-") and first not in LONG_TERMUX and not first.startswith("termux-mcp"):
        return min(TERMUX_API_TIMEOUT, base) if base > 0 else TERMUX_API_TIMEOUT
    return base if base > 0 else int(default or 0)


def popen_kwargs() -> dict:
    return {"start_new_session": True} if hasattr(os, "setsid") else {}


def arm_watchdog(process, limit: int, on_timeout) -> Optional[threading.Thread]:
    if not limit or limit <= 0:
        return None

    def _watch():
        import subprocess
        try:
            process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                on_timeout()
            finally:
                kill_group(process)

    thread = threading.Thread(target=_watch, daemon=True, name="kernel-watchdog")
    thread.start()
    return thread


def kill_group(process) -> None:
    import signal
    import subprocess
    if process is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, sig)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            process.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            continue


def authorize_tool(name: str, params=None, principal=None) -> str:
    if principal is None:
        principal = current_principal()
    reason = policy.check_tool(name)
    if reason:
        return reason
    if principal is None:
        return ""
    if principal.expired():
        return "capability token expired"
    inner = name
    if name == "smart" and isinstance(params, dict):
        inner = str(params.get("tool", ""))
        params = params.get("params") if isinstance(params.get("params"), dict) else {}
    if not (principal.allows_tool(name) or principal.allows_tool(inner)):
        return f"capability token does not allow {inner}"
    if principal.read_only and inner not in ("run", "run_digest") and is_write_call(inner, params):
        return f"capability token is read-only; {inner} changes things"
    if principal.paths is not None and isinstance(params, dict):
        for key in PATH_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value and os.path.isabs(os.path.expanduser(value)) \
                    and not principal.allows_path(value):
                return f"capability token does not cover {value}"
    return ""


PATH_KEYS = ("path", "file", "dest", "src", "cwd", "dir", "input", "output", "image", "target", "root")

WRITE_TOOLS = frozenset({
    "run", "write", "delete", "mkdir", "patch", "move", "copy", "cron-add", "cron_add", "service-guard",
    "run_digest", "pkg_ensure", "file_edit", "service_ensure", "service_stop", "port_free", "project_run",
    "storage_clean", "git_sync", "fix_apply", "tx_rollback", "watch_add", "watch_remove", "watch_toggle",
    "sandbox_apply", "restore_env", "recipe_share", "task_start", "learn_save", "fix_learn", "fix_share",
    "plan_replay", "goal_run", "policy_set", "cap_issue", "cap_revoke", "undo_task", "venv_ensure",
    "node_ensure", "proot_ensure", "proot_run", "cron_ensure", "tunnel", "tunnel_stop", "db_query",
    "log_watch", "backup_incremental", "clipboard_pipe", "share_to", "media_convert", "ssh_hosts",
    "ssh_run", "terminal_open", "terminal_send", "approve", "undo", "do", "playbook_run", "harvest",
    "plan_cache_put",
})


_READ_MODES = {
    "db_query": lambda p: not p.get("write"),
    "clipboard_pipe": lambda p: str(p.get("direction", "get")) == "get",
    "ssh_hosts": lambda p: str(p.get("action", "list")) == "list",
    "log_watch": lambda p: str(p.get("action", "scan")) == "scan",
    "backup_incremental": lambda p: str(p.get("action", "backup")) in ("list", "verify"),
    "cron_ensure": lambda p: str(p.get("action", "ensure")) == "list",
    "tunnel": lambda p: str(p.get("action", "open")) == "list",
    "restore_env": lambda p: p.get("dry_run", True) not in (False, "false", 0),
    "storage_clean": lambda p: p.get("dry_run", True) not in (False, "false", 0),
}


def is_write_call(name: str, params=None) -> bool:
    if name not in WRITE_TOOLS:
        return False
    p = params if isinstance(params, dict) else {}
    if p.get("dry_run") in (True, "true", 1):
        return False
    probe = _READ_MODES.get(name)
    return not (probe and probe(p))


def looks_mutating(cmd: str) -> bool:
    from .safety import write_targets
    text = str(cmd or "")
    if _MUTATING.search(text):
        return True
    try:
        return bool(write_targets(text, include_removals=True))
    except Exception:
        return True


def _decision(status: str, message: str = "", risk_level: str = "", code: str = "") -> dict:
    return {"allow": status == "ok", "status": status, "message": message, "risk_level": risk_level,
            "code": code or {"ok": "", "blocked": "E_BLOCKED", "denied": "E_POLICY",
                             "confirm": "E_CONFIRM"}[status],
            "http": 403 if status in ("blocked", "denied") else 200}


def gate(cmd: str, confirmed: bool = False, tool: str = "run", principal=None,
         enforce_warning: bool = True, cwd: str = "", spend_approval: bool = True) -> dict:
    cmd = str(cmd or "").strip()
    if principal is None:
        principal = current_principal()
    if principal is not None:
        if principal.expired():
            return _decision("denied", "capability token expired", code="E_CAPABILITY")
        if principal.read_only and looks_mutating(cmd):
            return _decision("denied", "capability token is read-only", code="E_CAPABILITY")
        if principal.paths is not None:
            from .policy import _PATH_TOKEN, _resolve
            for token in _PATH_TOKEN.findall(cmd):
                real = _resolve(token, cwd)
                if real.startswith(("/dev/", "/proc/")) or real in ("/dev/null",):
                    continue
                if not principal.allows_path(real):
                    return _decision("denied", f"capability token does not cover {token}", code="E_CAPABILITY")
    reason = policy.check_command(cmd, tool, cwd)
    if reason:
        return _decision("denied", reason)
    risk = get_risk_assessment(cmd)
    if risk["blocked"]:
        return _decision("blocked", risk["message"], risk["risk_level"])
    if risk["requires_confirmation"] and enforce_warning and not confirmed:
        from . import approval
        if not (spend_approval and approval.spend(cmd)):
            return _decision("confirm", risk["message"], risk["risk_level"])
    return _decision("ok", "", risk.get("risk_level", ""))


def confirmation_payload(cmd: str, decision: dict) -> dict:
    return {"status": "confirmation_required", "command": cmd,
            "risk_level": decision["risk_level"], "message": decision["message"],
            "requires_confirmation": True,
            "hint": "Re-send with confirmed: true, or approve this exact "
                    "command on the device with the approve tool first."}


def refusal_payload(decision: dict) -> dict:
    body = {"error": decision["message"], "risk_level": decision["risk_level"], "code": decision["code"]}
    if decision["status"] == "blocked":
        body["blocked"] = True
    else:
        body["denied"] = True
    return body


def prepare(cmd: str, task_id: str = "", echo: bool = True, record: bool = True):
    from .safety import snapshot_targets_from_command
    from .utils import shell_quote
    try:
        snaps = snapshot_targets_from_command(cmd, task_id or "")
    except Exception:
        snaps = []
    if record:
        record_action(task_id, cmd)
    if echo and snaps and not cmd.strip().startswith("cd"):
        hint = "; ".join(f"snapshot: {s}" for s in snaps)
        cmd = f"echo {shell_quote(hint)}; {cmd}"
    return cmd, snaps


_actions_lock = threading.Lock()
ACTION_TASKS_KEPT = 40
ACTIONS_PER_TASK = 60


def record_action(task_id: str, cmd: str, ok: Optional[bool] = None, tool: str = "run") -> None:
    if not task_id:
        return
    try:
        from .smart import base
        with _actions_lock:
            state = base.load_state("actions", {})
            rows = state.setdefault(str(task_id), [])
            rows.append({"tool": tool, "cmd": str(cmd)[:2000], "ok": ok})
            state[str(task_id)] = rows[-ACTIONS_PER_TASK:]
            if len(state) > ACTION_TASKS_KEPT:
                for old in list(state)[:len(state) - ACTION_TASKS_KEPT]:
                    state.pop(old, None)
            base.save_state("actions", state)
    except Exception:
        pass


def actions_for(task_id: str) -> list:
    from .smart import base
    return list(base.load_state("actions", {}).get(str(task_id), []))
