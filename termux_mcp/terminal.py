import errno
import os
import re
import select
import signal
import struct
import threading
import time
import uuid

try:
    import fcntl
    import pty
    import termios
except ImportError:  # pragma: no cover
    fcntl = pty = termios = None  # type: ignore[assignment]

_POSIX = pty is not None and hasattr(os, "fork") and hasattr(os, "setsid")

from .config import (HOME, TERMINAL_IDLE_TIMEOUT, TERMINAL_MAX_SESSIONS,
                     TERMINAL_RING_BYTES)

_POLL_SECONDS = 0.5

_READ_CHUNK = 8192

_DEFAULT_SHELLS = (
    "/data/data/com.termux/files/usr/bin/bash",
    "/system/bin/sh",
)


# ── Helpers ──────────────────────────────────────────────────────────────


_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-9;?]*[ -/]*[@-~]
  | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)
  | \x1b[@-Z\\-_]
  | [\x00-\x08\x0b-\x1f\x7f]
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def trim_to_char_boundary(data: bytes) -> bytes:
    i = 0
    while i < len(data) and i < 4 and 0x80 <= data[i] < 0xC0:
        i += 1
    return data[i:]


def find_shell() -> str:
    env_shell = os.environ.get("SHELL", "")
    if env_shell and os.path.exists(env_shell):
        return env_shell
    for candidate in _DEFAULT_SHELLS:
        if os.path.exists(candidate):
            return candidate
    return "sh"


INTERACTIVE_PROGRAMS = frozenset({
    "cmatrix", "vim", "vi", "nvim", "nano", "emacs", "pico",
    "htop", "top", "btop", "atop", "less", "more", "man",
    "fzf", "lazygit", "lazydocker", "tmux", "screen",
    "ssh", "mosh", "telnet",
    "mysql", "psql", "sqlite3", "mongo", "redis-cli",
    "python", "python3", "ipython", "node", "irb", "ghci", "lua", "ruby",
    "gdb", "lldb", "pdb", "ncdu", "ranger", "nnn", "mc", "watch", "dialog",
    "whiptail", "passwd", "su",
})

_PREFIX_WORDS = frozenset({
    "sudo", "doas", "env", "nice", "ionice", "time", "command", "exec",
    "nohup", "setsid", "stdbuf", "taskset", "chrt", "timeout",
})


def _is_number(word: str) -> bool:
    return word.replace(".", "").replace(",", "").isdigit()


def interactive_program(cmd: str):
    if not cmd:
        return None

    for segment in re.split(r"[;&|]+", cmd):
        past_prefix = False

        for word in segment.split():
            if not past_prefix and "=" in word and not word.startswith("-"):
                continue
            name = os.path.basename(word)
            if name in _PREFIX_WORDS:
                past_prefix = True
                continue
            if past_prefix and (word.startswith("-") or _is_number(word)):
                continue
            if name in INTERACTIVE_PROGRAMS:
                return name
            break

    return None


# ── Session ──────────────────────────────────────────────────────────────


class TerminalSession:

    def __init__(self, session_id: str, pid: int, master_fd: int,
                 shell: str, cwd: str, cols: int, rows: int) -> None:
        self.id = session_id
        self.pid = pid
        self.master_fd = master_fd
        self.shell = shell
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.exit_code = None
        self.closed = False

        self._lock = threading.RLock()
        self._ring = bytearray()
        self._sinks = {}
        self._exit_sinks = {}
        self._reader = None

    # ── Output path ─────────────────────────────────────────────────────

    def attach(self, send_output, send_exit):
        token = uuid.uuid4().hex
        with self._lock:
            self._sinks[token] = send_output
            self._exit_sinks[token] = send_exit
            replay = bytes(self._ring)
            already_dead = self.closed
            code = self.exit_code

        if replay and not already_dead:
            try:
                send_output(trim_to_char_boundary(replay))
            except Exception:
                pass

        if already_dead:
            try:
                send_exit(code)
            except Exception:
                pass

        return token

    def detach(self, token) -> None:
        with self._lock:
            self._sinks.pop(token, None)
            self._exit_sinks.pop(token, None)

    def is_attached(self) -> bool:
        with self._lock:
            return bool(self._sinks)

    def _publish(self, data: bytes) -> None:
        with self._lock:
            self._ring.extend(data)
            overflow = len(self._ring) - TERMINAL_RING_BYTES
            if overflow > 0:
                del self._ring[:overflow]
            sinks = list(self._sinks.values())

        for send in sinks:
            try:
                send(data)
            except Exception:
                pass

    # ── Input path ──────────────────────────────────────────────────────

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        with self._lock:
            fd = self.master_fd
            if self.closed or fd is None:
                return False
        try:
            os.write(fd, data)
            self.last_activity = time.time()
            return True
        except OSError:
            return False

    def resize(self, cols: int, rows: int) -> bool:
        if cols <= 0 or rows <= 0:
            return False
        with self._lock:
            fd = self.master_fd
            if self.closed or fd is None:
                return False
            self.cols, self.rows = cols, rows
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            return True
        except OSError:
            return False

    # ── Reader ──────────────────────────────────────────────────────────

    def start_reader(self) -> None:
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"pty-{self.id}", daemon=True
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        while True:
            with self._lock:
                fd = self.master_fd
                if self.closed or fd is None:
                    return

            try:
                ready, _, _ = select.select([fd], [], [], _POLL_SECONDS)
            except (OSError, ValueError):
                return

            if ready:
                try:
                    data = os.read(fd, _READ_CHUNK)
                except OSError as e:
                    if e.errno == errno.EINTR:
                        continue
                    break
                if not data:
                    break
                self._publish(data)
                continue

            if self._reap_if_exited():
                break

        self._finish()

    def _reap_if_exited(self) -> bool:
        with self._lock:
            if self.closed:
                return True
            pid = self.pid
        try:
            reaped, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return False

        if reaped == 0:
            return False

        self.exit_code = self._decode_status(status)
        return True

    @staticmethod
    def _decode_status(status: int):
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return None

    def _finish(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            fd = self.master_fd
            self.master_fd = None
            exit_sinks = list(self._exit_sinks.values())
            self._sinks.clear()
            self._exit_sinks.clear()
            code = self.exit_code

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        if code is None:
            try:
                _, status = os.waitpid(self.pid, os.WNOHANG)
                code = self._decode_status(status)
                self.exit_code = code
            except (ChildProcessError, OSError):
                pass

        for send_exit in exit_sinks:
            try:
                send_exit(code)
            except Exception:
                pass

        TerminalManager.remove(self.id)

    # ── Teardown ────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            pid = self.pid

        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            for _ in range(20):
                if self._reap_if_exited():
                    break
                time.sleep(0.05)
            else:
                continue
            break

        self._finish()

    def snapshot(self, limit: int = 4000) -> str:
        with self._lock:
            raw = bytes(self._ring[-limit:]) if self._ring else b""
        return strip_ansi(raw.decode("utf-8", errors="replace"))

    def info(self) -> dict:
        with self._lock:
            attached = bool(self._sinks)
        return {
            "id": self.id,
            "pid": self.pid,
            "shell": self.shell,
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
            "running": not self.closed,
            "attached": attached,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
        }


# ── Manager ──────────────────────────────────────────────────────────────


class TerminalManager:

    _sessions = {}
    _lock = threading.Lock()

    @classmethod
    def create(cls, shell=None, cols=80, rows=24, cwd=None):
        if not _POSIX:
            raise RuntimeError(
                "Interactive terminals need a POSIX platform (Termux/Linux): "
                "fork, pty and termios are required."
            )

        with cls._lock:
            if len(cls._sessions) >= TERMINAL_MAX_SESSIONS:
                raise RuntimeError(
                    f"Terminal limit reached ({TERMINAL_MAX_SESSIONS}). "
                    "Close a session first."
                )

        shell = shell or find_shell()
        cwd = cwd or HOME
        if not os.path.isdir(cwd):
            cwd = HOME

        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))

        master_fd, slave_fd = pty.openpty()

        try:
            fcntl.ioctl(
                slave_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

        pid = os.fork()
        if pid == 0:
            try:
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                os.environ["TERM"] = "xterm-256color"
                os.environ["COLORTERM"] = "truecolor"
                os.execvpe(shell, [os.path.basename(shell), "-l"], os.environ)
            except BaseException:
                pass
            os._exit(127)

        os.close(slave_fd)

        session = TerminalSession(
            session_id="term_" + uuid.uuid4().hex[:8],
            pid=pid,
            master_fd=master_fd,
            shell=shell,
            cwd=cwd,
            cols=cols,
            rows=rows,
        )

        with cls._lock:
            cls._sessions[session.id] = session

        session.start_reader()
        return session

    @classmethod
    def get(cls, session_id: str):
        with cls._lock:
            return cls._sessions.get(session_id)

    @classmethod
    def list(cls):
        with cls._lock:
            return [s.info() for s in cls._sessions.values()]

    @classmethod
    def remove(cls, session_id: str) -> None:
        with cls._lock:
            cls._sessions.pop(session_id, None)

    @classmethod
    def close(cls, session_id: str) -> bool:
        session = cls.get(session_id)
        if session is None:
            return False
        session.close()
        return True

    @classmethod
    def close_all(cls) -> None:
        with cls._lock:
            sessions = list(cls._sessions.values())
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    @classmethod
    def reap_idle(cls) -> None:
        if TERMINAL_IDLE_TIMEOUT <= 0:
            return
        cutoff = time.time() - TERMINAL_IDLE_TIMEOUT
        with cls._lock:
            candidates = [s for s in cls._sessions.values() if not s.is_attached()]
        for session in candidates:
            if session.last_activity < cutoff:
                session.close()
