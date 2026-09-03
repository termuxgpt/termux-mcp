import logging
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional


from . import mcp_config as cfg
from . import mcp_transport_stdio as stdio
from .mcp_transport_http import MCPRequestHandler
from .network import kill_port


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class MCPHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _check_startup_guards(host: str) -> None:
    if cfg.require_auth() and len(cfg.native_auth_token()) < 16:
        logger.error(
            "Auth token is set but too short (< 16 chars). Refusing to "
            "start for safety."
        )
        sys.exit(1)
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.require_auth():
        logger.error(
            "HOST is set to %s (non-loopback) but no auth token is set. "
            "Refusing to start. Set TERMUX_NATIVE_MCP_AUTH_TOKEN (or "
            "TERMUX_MCP_AUTH_TOKEN) or bind to 127.0.0.1.", host,
        )
        sys.exit(1)


def run_http(host: Optional[str] = None, port: Optional[int] = None) -> None:
    host = host or cfg.native_host()
    port = int(port if port is not None else cfg.native_port())
    _check_startup_guards(host)

    MCPRequestHandler.endpoint = cfg.native_path()

    logger.info("Freeing port %d if occupied...", port)
    kill_port(port)

    server = MCPHTTPServer((host, port), MCPRequestHandler)
    if cfg.require_auth():
        logger.info("Authentication: enabled (length=%d)",
                    len(cfg.native_auth_token()))
    logger.info("Termux native MCP (Streamable HTTP) listening on "
                "http://%s:%d%s", host, port, cfg.native_path())
    logger.info("REST API (termux-mcp) is a separate process — "
                "unchanged on its own port.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()
        sys.exit(0)


def run_stdio() -> None:
    logger.info("Termux native MCP (stdio) — parent process drives "
                "stdin/stdout. Nothing on stdout but MCP messages.\n")
    try:
        stdio.serve_stdio()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    sys.exit(0)
