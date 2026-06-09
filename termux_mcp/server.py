import logging
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from .config import AUTH_TOKEN, HOST, PORT, REQUIRE_AUTH
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
    if REQUIRE_AUTH:
        if len(AUTH_TOKEN) < 16:
            logger.error(
                "TERMUX_MCP_AUTH_TOKEN is set but too short (< 16 chars). "
                "Refusing to start for safety."
            )
            sys.exit(1)
        logger.info("Auth token configured (length=%d)", len(AUTH_TOKEN))

    if HOST != "127.0.0.1" and HOST != "localhost" and not REQUIRE_AUTH:
        logger.error(
            "HOST is set to %s (non-loopback) but TERMUX_MCP_AUTH_TOKEN "
            "is not set. Refusing to start — network-exposed shell execution "
            "requires authentication. Set TERMUX_MCP_AUTH_TOKEN or bind to 127.0.0.1.",
            HOST,
        )
        sys.exit(1)

    logger.info("Freeing port %d if occupied...", PORT)
    kill_port(PORT)

    server = ThreadingHTTPServer((HOST, PORT), MCPHandler)

    logger.info("TermuxMCP running on http://%s:%d", HOST, PORT)
    logger.info("Working dir: %s", get_current_dir())
    if REQUIRE_AUTH:
        logger.info("Authentication: enabled")
    logger.info("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()
        sys.exit(0)
