import os

PORT: int = int(os.environ.get("TERMUX_MCP_PORT", 8080))
HOST: str = os.environ.get("TERMUX_MCP_HOST", "127.0.0.1")

HOME: str = os.environ.get("HOME", "/data/data/com.termux/files/home")

COMMAND_TIMEOUT: int = int(os.environ.get("TERMUX_MCP_TIMEOUT", "0"))

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

TERMINAL_MAX_SESSIONS: int = int(os.environ.get("TERMUX_MCP_MAX_TERMINALS", "3"))

TERMINAL_RING_BYTES: int = int(os.environ.get("TERMUX_MCP_TERMINAL_RING", str(1024 * 1024)))

TERMINAL_IDLE_TIMEOUT: int = int(os.environ.get("TERMUX_MCP_TERMINAL_IDLE", "0"))

TERMINAL_READ_BYTES: int = int(os.environ.get("TERMUX_MCP_TERMINAL_READ", "4000"))
