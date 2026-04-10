import logging
import sys
from http.server import HTTPServer

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


def run() -> None:
    logger.info("Freeing port %d if occupied…", PORT)
    kill_port(PORT)

    server = HTTPServer((HOST, PORT), MCPHandler)

    logger.info("🚀 TermuxMCP running on http://localhost:%d", PORT)
    logger.info("📂 Working dir: %s", get_current_dir())
    logger.info("Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        server.server_close()
        sys.exit(0)
