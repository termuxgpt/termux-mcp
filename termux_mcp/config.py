import os

PORT: int = int(os.environ.get("TERMUX_MCP_PORT", 8080))
HOST: str = os.environ.get("TERMUX_MCP_HOST", "127.0.0.1")

HOME: str = os.environ.get("HOME", "/data/data/com.termux/files/home")

COMMAND_TIMEOUT: int = int(os.environ.get("TERMUX_MCP_TIMEOUT", 120))

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
