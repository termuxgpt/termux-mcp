import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp.smart import hardening


@pytest.fixture
def home():
    h = tempfile.mkdtemp(prefix="hard-")
    yield h
    shutil.rmtree(h, ignore_errors=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def test_secrets_are_found_masked_and_skip_noise(home):
    write(f"{home}/bot/config.py", "TOKEN = 'ghp_" + "a" * 36 + "'\n")
    write(f"{home}/bot/.env", "OPENAI=sk-" + "b" * 30 + "\n")
    write(f"{home}/bot/node_modules/x.js", "AKIA" + "C" * 16)
    write(f"{home}/.ssh/id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\n")
    found = hardening.scan_secrets(home)
    kinds = sorted(f["kind"] for f in found)
    assert kinds == ["GitHub token", "OpenAI-style key"]
    assert all("a" * 10 not in f["preview"] and "b" * 10 not in f["preview"] for f in found)


def test_listeners_ignore_localhost():
    text = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:(("python3",pid=1,fd=3))
LISTEN 0 128 0.0.0.0:8022 0.0.0.0:* users:(("sshd",pid=2,fd=3))
LISTEN 0 128 [::]:5000 [::]:* users:(("node",pid=3,fd=3))"""
    got = hardening.parse_listeners(text)
    assert [(p["process"], p["port"]) for p in got] == [("sshd", 8022), ("node", 5000)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_loose_private_keys(home):
    k = write(f"{home}/.ssh/id_ed25519", "x")
    os.chmod(k, 0o644)
    write(f"{home}/.ssh/id_ed25519.pub", "x")
    [bad] = hardening.loose_keys(home)
    assert bad["file"] == k and bad["fix"] == f"chmod 600 {k}"
