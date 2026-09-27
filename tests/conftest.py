import os
import shutil
import tempfile

import pytest

_CONFIG = tempfile.mkdtemp(prefix="termux-mcp-config-")
os.environ["TERMUX_MCP_AUTH"] = "off"
os.environ["TERMUX_MCP_CONFIG_DIR"] = _CONFIG
os.environ.pop("TERMUX_MCP_AUTH_TOKEN", None)


@pytest.fixture(autouse=True)
def _clean_policy():
    path = os.path.join(_CONFIG, "policy.json")
    if os.path.exists(path):
        os.remove(path)
    yield
    if os.path.exists(path):
        os.remove(path)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_CONFIG, ignore_errors=True)
