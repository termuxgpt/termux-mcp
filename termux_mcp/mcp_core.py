import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable, Optional


from . import mcp_bridge
from . import mcp_config as cfg
from .config import COMMAND_TIMEOUT, HOME, MAX_OUTPUT_BYTES
from .safety import snapshot_targets_from_command
from .security import get_risk_assessment
from .shell import preprocess, set_current_dir
from .utils import shell_quote
from .websocket import _session_capture, _spawn_auto_input


logger = logging.getLogger(__name__)


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


DEFAULT_TMUX_SESSION = "termux-mcp"
_TRUNCATION_MARKER = (
    f"\n[Output truncated: max {MAX_OUTPUT_BYTES} bytes — "
    f"full output not sent]\n"
)


NATIVE_TOOL_DEFS = [
    {
        "name": "run",
        "description": (
            "Execute a shell command in Termux with streaming output. "
            "Maintains persistent cd state for this MCP session. Long jobs: "
            "use session_run/session_poll instead. Risky commands are "
            "blocked or need confirmed: true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command"},
                "confirmed": {"type": "boolean",
                              "description": "Acknowledge a risk warning",
                              "default": False},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "cancel",
        "description": "Cancel the command currently running in this session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "session_start",
        "description": "Start (or ensure) a persistent tmux session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string",
                            "description": "Session name",
                            "default": DEFAULT_TMUX_SESSION},
            },
        },
    },
    {
        "name": "session_run",
        "description": (
            "Run a command in a tmux session WITHOUT blocking — returns "
            "immediately with initial output; poll with session_poll."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string",
                            "description": "Session name",
                            "default": DEFAULT_TMUX_SESSION},
                "cmd": {"type": "string",
                        "description": "Command to run in the session"},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "session_poll",
        "description": "Get new output from a running tmux session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string",
                            "description": "Session name",
                            "default": DEFAULT_TMUX_SESSION},
            },
        },
    },
    {
        "name": "session_list",
        "description": "List live tmux sessions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "session_kill",
        "description": "Kill a tmux session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string",
                            "description": "Session name",
                            "default": DEFAULT_TMUX_SESSION},
            },
        },
    },
]


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPSession:

    def __init__(self, sid: str, transport: str) -> None:
        self.sid = sid
        self.transport = transport
        self.cwd = HOME
        self.created = time.time()
        self.protocol_version: Optional[str] = None
        self.lock = threading.Lock()
        self.killed = threading.Event()
        self.closed = threading.Event()
        self.active_pid: Optional[int] = None
        self.on_line: Optional[Callable[[str], None]] = None
        self.tmux_seen = {}
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def expired(self, ttl: float) -> bool:
        return time.monotonic() - self.last_used > ttl

    def kill_active(self) -> bool:
        pid = self.active_pid
        if pid is None:
            return False
        return _kill_process_group(pid)


def _kill_process_group(pid: int) -> bool:
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    try:
        if killpg is not None and getpgid is not None:
            killpg(getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError):
        return False


_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def create_session(transport: str) -> MCPSession:
    sid = uuid.uuid4().hex
    session = MCPSession(sid, transport)
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = session
    return session


def get_session(sid: str, touch: bool = True):
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(sid)
        if session is None:
            return None
        if session.expired(cfg.session_ttl()):
            _SESSIONS.pop(sid, None)
            return None
    if touch:
        session.touch()
    return session


def drop_session(sid: str) -> None:
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(sid, None)
    if session is not None:
        session.killed.set()
        session.kill_active()
        session.closed.set()


def active_session_count() -> int:
    with _SESSIONS_LOCK:
        return len(_SESSIONS)


def server_info() -> dict:
    return {
        "protocolVersion": cfg.DEFAULT_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": cfg.SERVER_NAME, "version": cfg.SERVER_VERSION},
        "instructions": (
            "Termux shell + device tools on this Android device. Commands "
            "stream output; risky commands are blocked or require "
            "confirmed:true. Deletes move to ~/termuxGPT/trash and file "
            "overwrites are snapshotted under ~/termuxGPT/snapshots. "
            "For long jobs use session_run then session_poll."
        ),
    }


def negotiate_protocol(client_version) -> str:
    if client_version in cfg.SUPPORTED_PROTOCOL_VERSIONS:
        return client_version
    return cfg.DEFAULT_PROTOCOL_VERSION


def _cd_into(session: MCPSession, raw_cmd: str):
    rest = raw_cmd[2:].strip()
    path_part, chained = rest, None
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1:
            path_part = rest[:idx].strip()
            chained = rest[idx + len(sep):].strip()
            break
    if not path_part or path_part == "~":
        session.cwd = HOME
        return True, HOME, chained
    raw_path = path_part.replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path)
        else os.path.join(session.cwd, raw_path)
    )
    if os.path.isdir(new_path):
        session.cwd = new_path
        return True, session.cwd, chained
    return False, f"Directory not found: {new_path}", None


def _notify(session: MCPSession, line: str) -> None:
    cb = session.on_line
    if cb is not None:
        try:
            cb(line)
        except Exception:
            session.on_line = None


def _execute_command(session: MCPSession, raw_cmd: str) -> dict:
    session.killed.clear()
    process = None
    watchdog = None
    sent_bytes = 0
    truncated = False
    chunks = []

    def append(text: str) -> None:
        chunks.append(text)
        _notify(session, text)

    try:
        setsid = getattr(os, "setsid", None)
        kwargs = {"shell": True, "stdout": subprocess.PIPE,
                  "stderr": subprocess.STDOUT, "stdin": subprocess.PIPE,
                  "text": True, "cwd": session.cwd}
        if setsid is not None:
            kwargs["preexec_fn"] = setsid
        process = subprocess.Popen(
            f"export PAGER=cat; {preprocess(raw_cmd)}", **kwargs
        )
        session.active_pid = process.pid
        _spawn_auto_input(process, raw_cmd)

        if COMMAND_TIMEOUT > 0:
            def _watchdog():
                try:
                    process.wait(timeout=COMMAND_TIMEOUT)
                except subprocess.TimeoutExpired:
                    session.killed.set()
                    session.kill_active()
                    try:
                        process.kill()
                    except Exception:
                        pass
            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.start()

        stdout = process.stdout
        if stdout is None:
            append("\n❌ Error: no stdout pipe\n")
            process.wait()
        for line in stdout or []:
            if session.killed.is_set():
                append("\nCancelled\n")
                break
            sent_bytes += len(line.encode())
            if sent_bytes <= MAX_OUTPUT_BYTES:
                append(line)
            elif not truncated:
                truncated = True
                append(_TRUNCATION_MARKER)

        if watchdog is not None:
            watchdog.join(timeout=2)

        if not session.killed.is_set():
            if process.returncode is None:
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
            if process.returncode is None:
                append("\nExit: (process detached)\n")
            elif process.returncode == 0:
                append("\n✅ Done\n")
            else:
                append(f"\n❌ Exit code: {process.returncode}\n")
    except Exception as e:
        append(f"\n❌ Error: {e}\n")
    finally:
        session.active_pid = None

    meta = mcp_bridge.analyze_output("".join(chunks))
    return {"text": meta["text"], "is_error": _error_from_meta(meta)}


def _error_from_meta(meta: dict) -> bool:
    code = meta["exit_code"]
    return meta["error"] or meta["timed_out"] or (code not in (None, 0))


def _tool_run(session: MCPSession, params: dict) -> dict:
    p = params or {}
    cmd = str(p.get("cmd", "")).strip()
    confirmed = bool(p.get("confirmed", False))
    if not cmd:
        return {"text": "Missing 'cmd'", "is_error": True}

    risk = get_risk_assessment(cmd)
    if risk["blocked"]:
        return {"text": risk["message"], "is_error": True}
    if risk["requires_confirmation"] and not confirmed:
        return {
            "text": (risk["message"] + "\n\nRe-invoke with `confirmed: true` "
                     "to proceed."),
            "is_error": False,
        }

    snaps = snapshot_targets_from_command(cmd)
    if snaps and not cmd.startswith("cd"):
        hint = "; ".join(f"snapshot: {s}" for s in snaps)
        cmd = f"echo {shell_quote(hint)}; {cmd}"
    elif snaps:
        logger.info("Snapshots taken (cd command, not echoed): %s", snaps)

    if cmd.startswith("cd"):
        ok, msg, chained = _cd_into(session, cmd)
        if not ok:
            return {"text": str(msg), "is_error": True}
        if chained is None:
            return {"text": str(msg), "is_error": False}
        return _execute_command(session, chained)
    return _execute_command(session, cmd)


def _tool_cancel(session: MCPSession) -> dict:
    session.killed.set()
    ok = session.kill_active()
    return {"text": ("Cancelled." if ok else "Nothing running."),
            "is_error": False, "cancelled": ok}


def _ensure_tmux(session: MCPSession, name: str) -> None:
    alive = os.popen(
        f"tmux has-session -t {name} 2>/dev/null && echo yes || echo no"
    ).read().strip() == "yes"
    if not alive:
        os.system(f"tmux new-session -d -s {name} 2>/dev/null")
        os.system(f"tmux set-option -t {name} history-limit 20000 2>/dev/null")
    session.tmux_seen.setdefault(name, 0)


def _tool_session(session: MCPSession, name: str, params: dict) -> dict:
    p = params or {}
    sess_name = str(p.get("session") or DEFAULT_TMUX_SESSION)

    if name == "session_start":
        _ensure_tmux(session, sess_name)
        return {"text": f"Session '{sess_name}' ready.", "is_error": False}

    if name == "session_list":
        out = os.popen(
            "tmux list-sessions 2>/dev/null || echo 'No sessions (tmux not installed?)'"
        ).read().strip()
        return {"text": out, "is_error": False}

    if name == "session_kill":
        os.system(f"tmux kill-session -t {sess_name} 2>/dev/null")
        session.tmux_seen.pop(sess_name, None)
        return {"text": f"Killed: {sess_name}", "is_error": False}

    if name == "session_run":
        cmd = str(p.get("cmd", "")).strip()
        if not cmd:
            return {"text": "Missing cmd", "is_error": True}
        _ensure_tmux(session, sess_name)
        os.system(f"tmux send-keys -t {sess_name} {shell_quote(cmd)} Enter")
        time.sleep(1.2)
        seen = session.tmux_seen[sess_name]
        initial, session.tmux_seen[sess_name] = _session_capture(
            sess_name, seen)
        preview = initial.strip()
        if len(preview) > 2000:
            preview = preview[-2000:]
        return {"text": (
            f"Started in session '{sess_name}':\n{preview or '(no output yet — use session_poll)'}"
        ), "is_error": False}

    if name == "session_poll":
        _ensure_tmux(session, sess_name)
        seen = session.tmux_seen[sess_name]
        output, session.tmux_seen[sess_name] = _session_capture(
            sess_name, seen)
        alive = os.popen(
            f"tmux has-session -t {sess_name} 2>/dev/null && echo yes || echo no"
        ).read().strip() == "yes"
        preview = output.strip()
        if len(preview) > 4000:
            preview = preview[-4000:]
        return {"text": preview or "(no new output)",
                "is_error": False, "running": alive}

    return {"text": f"Unknown session tool: {name}", "is_error": True}


def invoke_tool(session: MCPSession, name: str, params: dict,
                on_progress=None) -> dict:
    p = params or {}
    if not isinstance(p, dict):
        raise RpcError(INVALID_PARAMS, "Tool arguments must be an object")

    if name == "cancel":
        return _tool_cancel(session)

    old_cb = session.on_line
    session.on_line = on_progress
    try:
        if name in NATIVE_NAMES:
            with session.lock:
                if name == "run":
                    return _tool_run(session, p)
                return _tool_session(session, name, p)

        route = mcp_bridge.route_callable(name)
        if route is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        with session.lock:

            set_current_dir(session.cwd)
            vh = mcp_bridge.VirtualHandler()
            route(vh, p)
            return mcp_bridge.decode_virtual(vh)
    finally:
        session.on_line = old_cb


NATIVE_NAMES = frozenset(d["name"] for d in NATIVE_TOOL_DEFS)


def tool_list() -> dict:
    tools = mcp_bridge.build_mcp_tool_list(NATIVE_TOOL_DEFS,
                                           cfg.tools_filter())
    return {"tools": tools}


def check_tool_allowed(name: str) -> None:
    if not mcp_bridge.tool_name_allowed(name, cfg.tools_filter()):
        raise RpcError(INVALID_PARAMS, f"Tool is disabled: {name}")


def call_tool(session: MCPSession, name: str, params: dict,
              on_progress=None) -> dict:
    if not isinstance(name, str) or not name:
        raise RpcError(INVALID_PARAMS, "Missing tool name")
    check_tool_allowed(name)
    result = invoke_tool(session, name, params, on_progress)
    content = []
    text = result.get("text", "")
    if text:
        content.append({"type": "text", "text": text})
    if not content:
        content.append({"type": "text", "text": "(no output)"})
    out: dict = {"content": content}
    if result.get("is_error"):
        out["isError"] = True
    for key in ("cancelled", "running"):
        if key in result:
            out[key] = result[key]
    return out


def dispatch(session: MCPSession, method: str, params,
             on_progress=None):
    if not isinstance(method, str) or not method:
        raise RpcError(INVALID_REQUEST, "Missing method")
    session.touch()

    if method == "initialize":
        client_v = (params or {}).get("protocolVersion") if isinstance(
            params, dict) else None
        session.protocol_version = negotiate_protocol(client_v)
        return server_info()
    if method == "ping":
        return {}
    if method == "tools/list":
        return tool_list()
    if method == "tools/call":
        params = params or {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "Invalid parameters")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "Missing tool name")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "Tool arguments must be an object")
        progress = None
        meta = args.get("_meta") if isinstance(args, dict) else None
        if isinstance(meta, dict) and meta.get("progressToken") is not None:
            progress = _ProgressProxy(session, meta["progressToken"],
                                      on_progress)
        return call_tool(session, name, args, on_progress=progress)
    if method == "notifications/initialized":
        return None
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


class _ProgressProxy:

    def __init__(self, session, token, emit) -> None:
        self.session = session
        self.token = token
        self.emit = emit

    def __call__(self, line: str) -> None:
        if self.emit is not None:
            try:
                self.emit({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": self.token,
                                      "progress": 0, "message": line.rstrip()}})
            except Exception:
                self.session.on_line = None
