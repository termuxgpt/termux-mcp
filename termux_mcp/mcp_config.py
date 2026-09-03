import os


from .config import AUTH_TOKEN as REST_AUTH_TOKEN


SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18",)
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


SERVER_NAME = "termux-native-mcp"
SERVER_VERSION = "0.10.0"


def native_port() -> int:
    return int(os.environ.get("TERMUX_NATIVE_MCP_PORT", "8081"))


def native_host() -> str:
    return os.environ.get("TERMUX_NATIVE_MCP_HOST", "127.0.0.1")


def native_path() -> str:
    return os.environ.get("TERMUX_NATIVE_MCP_PATH", "/mcp")


def native_auth_token() -> str:
    return os.environ.get("TERMUX_NATIVE_MCP_AUTH_TOKEN") or REST_AUTH_TOKEN


def require_auth() -> bool:
    return bool(native_auth_token())


def origin_allowlist() -> list:
    raw = os.environ.get("TERMUX_NATIVE_MCP_ORIGIN", "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def session_ttl() -> float:
    return float(os.environ.get("TERMUX_NATIVE_MCP_SESSION_TTL", "1800"))


def tools_filter() -> tuple:
    raw = os.environ.get("TERMUX_NATIVE_MCP_TOOLS", "")
    includes, excludes = [], []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name.startswith("-"):
            excludes.append(name[1:].strip())
        else:
            includes.append(name)
    return includes, excludes
