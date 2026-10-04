import os
import shlex
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import base, cache, power, run_smart_tool


class Res:
    def __init__(self, ok=True):
        self.ok = ok


@pytest.fixture
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="doc-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    yield h
    shutil.rmtree(h, ignore_errors=True)


def touch(path, size=1000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


def test_rejects_unknown_action_and_missing_files(home):
    assert power.doc_tool("explode", [touch(f"{home}/a.pdf")])["errors"][0]["code"] == "E_ARGS"
    assert power.doc_tool("pdf_merge", [f"{home}/nope.pdf"], f"{home}/o.pdf")["errors"][0]["code"] == "E_NO_FILE"


def test_missing_binary_suggests_package(home, monkeypatch):
    monkeypatch.setattr(base, "which", lambda b: None)
    r = power.doc_tool("pdf_merge", [touch(f"{home}/a.pdf")], f"{home}/o.pdf")
    assert r["errors"][0]["code"] == "E_NOT_FOUND_CMD" and r["fixes"][0]["steps"] == ["pkg install -y qpdf"]


def test_merge_quotes_names_and_writes_output(home, monkeypatch):
    monkeypatch.setattr(base, "which", lambda b: "/bin/" + b)
    ran = []

    def fake_run(cmd, timeout=None, **k):
        ran.append(cmd)
        touch(f"{home}/out dir/merged.pdf")
        return Res(), {"ok": True}

    monkeypatch.setattr(power, "_run", fake_run)
    a, b = touch(f"{home}/my file.pdf"), touch(f"{home}/it's.pdf")
    r = power.doc_tool("pdf_merge", [a, b], "~/out dir/merged.pdf")
    assert r["ok"] and r["output"].endswith("merged.pdf")
    tokens = shlex.split(ran[0])
    assert tokens[:3] == ["qpdf", "--empty", "--pages"] and os.path.normpath(a) in tokens and os.path.normpath(b) in tokens


def test_compress_writes_next_to_originals_without_touching_them(home, monkeypatch):
    monkeypatch.setattr(base, "which", lambda b: "/bin/" + b)

    def fake_run(cmd, timeout=None, **k):
        dest = shlex.split(cmd)[-1]
        touch(dest, 300)
        return Res(), {"ok": True}

    monkeypatch.setattr(power, "_run", fake_run)
    src = touch(f"{home}/DCIM/IMG_1.png", 1000)
    r = power.doc_tool("image_compress", [src])
    assert r["ok"] and r["outputs"] == [os.path.join(home, "DCIM", "compressed", "IMG_1.jpg")]
    assert os.path.getsize(src) == 1000 and "70% smaller" in r["summary"]


def test_registered(home, monkeypatch):
    monkeypatch.setattr(base, "which", lambda b: None)
    assert "digest" in run_smart_tool("doc_tool", {"action": "pdf_text", "inputs": [touch(f"{home}/a.pdf")]})
