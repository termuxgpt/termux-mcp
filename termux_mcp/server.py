import logging
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from . import auth
from .config import HOST, PORT
from .handler import MCPHandler
from .network import kill_port
from .shell import get_current_dir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("token", "help", "-h", "--help"):
        sys.exit(auth.cli(sys.argv[1:]))

    token = auth.master_token()
    if token:
        if len(token) < 16:
            logger.error(
                "TERMUX_MCP_AUTH_TOKEN is set but too short (< 16 chars). "
                "Refusing to start for safety."
            )
            sys.exit(1)
        logger.info("Auth token configured (length=%d)", len(token))

    if HOST != "127.0.0.1" and HOST != "localhost" and not token:
        logger.error(
            "HOST is set to %s (non-loopback) but authentication is off. "
            "Refusing to start — set TERMUX_MCP_AUTH=on for network-exposed "
            "shell execution.",
            HOST,
        )
        sys.exit(1)

    logger.info("Freeing port %d if occupied...", PORT)
    kill_port(PORT)

    server = ThreadingHTTPServer((HOST, PORT), MCPHandler)

    logger.info("TermuxMCP running on http://%s:%d", HOST, PORT)
    logger.info("Working dir: %s", get_current_dir())
    if token:
        logger.info("Authentication: enabled")
    else:
        logger.info("Authentication: off — loopback only (TERMUX_MCP_AUTH=on to require a token)")
    logger.info("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()
        sys.exit(0)
