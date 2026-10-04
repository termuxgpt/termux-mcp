import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import base, cache, intent_tools, run_smart_tool


@pytest.fixture
def phone(monkeypatch):
    h = tempfile.mkdtemp(prefix="sr-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    shared = os.path.join(h, "storage", "shared")
    for rel in ("DCIM/Camera", "Download", "Android/data/app", "Android/media/com.whatsapp/WhatsApp/Media"):
        os.makedirs(os.path.join(shared, rel))
    yield h, shared
    shutil.rmtree(h, ignore_errors=True)


def blob(path, mb, age_days=0):
    with open(path, "wb") as f:
        f.truncate(int(mb * 1024 * 1024))
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


def test_report_ranks_folders_and_finds_big_and_old_files(phone):
    home, shared = phone
    blob(os.path.join(shared, "DCIM/Camera/vid.mp4"), 3)
    blob(os.path.join(shared, "Download/old.zip"), 2, age_days=200)
    blob(os.path.join(shared, "Download/new.pdf"), 1)
    blob(os.path.join(shared, "Android/data/app/cache.bin"), 5)
    blob(os.path.join(shared, "Android/media/com.whatsapp/WhatsApp/Media/a.jpg"), 1)
    r = intent_tools.storage_report(top=5, min_mb=2, old_days=90)
    names = [f["name"] for f in r["folders"]]
    assert r["shared_storage"] and names.index("DCIM") < names.index("Download")
    assert "WhatsApp media" in names
    big = [f["name"] for f in r["largest_files"]]
    assert big == ["vid.mp4", "old.zip"]
    assert r["old_downloads"]["count"] == 1


def test_no_shared_storage_is_reported(phone, monkeypatch):
    monkeypatch.setattr(intent_tools, "_shared_root", lambda: "")
    r = intent_tools.storage_report()
    assert not r["shared_storage"] and "termux-setup-storage" in r["summary"]


def test_registered_as_smart_tool(phone):
    r = run_smart_tool("storage_report", {"min_mb": 1000})
    assert "digest" in r
