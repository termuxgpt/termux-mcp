import os

PORT: int = int(os.environ.get("TERMUX_MCP_PORT", 8080))
HOST: str = os.environ.get("TERMUX_MCP_HOST", "127.0.0.1")

HOME: str = os.environ.get("HOME", "/data/data/com.termux/files/home")

# Command timeout in seconds. 0 (default) = NO timeout — long operations
# like pkg update/upgrade/install run until they finish. Set a positive
# value (e.g. 600) to re-enable the watchdog kill.
COMMAND_TIMEOUT: int = int(os.environ.get("TERMUX_MCP_TIMEOUT", "0"))

# Cap on streamed command output sent to clients. Output beyond this is
# drained (process keeps running) but discarded, with a truncation marker
# appended. Keeps LLM tool results small and token-efficient.
MAX_OUTPUT_BYTES: int = int(os.environ.get("TERMUX_MCP_MAX_OUTPUT", 20000))

AUTH_TOKEN: str = os.environ.get("TERMUX_MCP_AUTH_TOKEN", "")
REQUIRE_AUTH: bool = bool(AUTH_TOKEN)

AUTO_INPUT_INTERVAL: float = 0.5
PORT_POLL_INTERVAL: float = 0.3
AUTO_YES_COMMANDS: list[str] = [
    "pkg install",
    "pkg upgrade",
    "pkg update",
    "apt install",
    "apt upgrade",
    "apt update",
]

# ── Interactive terminal (PTY) ───────────────────────────────────────────

TERMINAL_MAX_SESSIONS: int = int(os.environ.get("TERMUX_MCP_MAX_TERMINALS", "3"))

TERMINAL_RING_BYTES: int = int(os.environ.get("TERMUX_MCP_TERMINAL_RING", str(1024 * 1024)))

TERMINAL_IDLE_TIMEOUT: int = int(os.environ.get("TERMUX_MCP_TERMINAL_IDLE", "0"))

TERMINAL_READ_BYTES: int = int(os.environ.get("TERMUX_MCP_TERMINAL_READ", "4000"))
