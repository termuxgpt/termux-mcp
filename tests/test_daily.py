import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import base, cache, daily, tool_cli


@pytest.fixture
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="daily-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    yield h
    shutil.rmtree(h, ignore_errors=True)


def fake_sh(outputs):
    def sh(cmd, **k):
        for key, out in outputs.items():
            if key in cmd:
                return base.ShellResult(cmd, 0, out, "", 0.0)
        return base.ShellResult(cmd, 1, "", "", 0.0)
    return sh


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_briefing_lists_battery_storage_updates(home, monkeypatch):
    monkeypatch.setattr(base, "sh", fake_sh({
        "termux-battery-status": json.dumps({"percentage": 81, "status": "DISCHARGING", "health": "GOOD", "temperature": 31.2}),
        "upgradable": "4",
    }))
    r = daily.briefing()
    assert r["lines"][0].startswith("Battery 81% discharging") and "31°C" in r["lines"][0]
    assert any("GB free" in l for l in r["lines"])
    assert "4 package update(s) available" in r["lines"]


def test_net_check_healthy(home, monkeypatch):
    monkeypatch.setattr(base, "sh", fake_sh({
        "ip route": "192.168.1.1",
        "ping": "rtt min/avg/max/mdev = 2.1/4.5/6.0/1.0 ms",
        "termux-wifi": json.dumps({"ssid": "home", "rssi": -50}),
    }))
    monkeypatch.setattr(daily.socket, "getaddrinfo", lambda *a: [])
    monkeypatch.setattr(daily.urllib.request, "urlopen", lambda *a, **k: Resp(b"x" * 1_000_000))
    r = daily.net_check(speed=True)
    assert r["verdict"] == "good" and r["checks"]["router_ms"] == 4.5
    assert r["checks"]["download_mbps"] is not None and r["wifi"]["ssid"] == "home"


def test_net_check_weak_wifi_and_slow_router(home, monkeypatch):
    monkeypatch.setattr(base, "sh", fake_sh({
        "ip route": "192.168.1.1",
        "ping": "rtt min/avg/max/mdev = 80/120.0/150/10 ms",
        "termux-wifi": json.dumps({"rssi": -82}),
    }))
    monkeypatch.setattr(daily.socket, "getaddrinfo", lambda *a: [])
    monkeypatch.setattr(daily.urllib.request, "urlopen", lambda *a, **k: Resp(b""))
    r = daily.net_check(speed=False)
    assert r["verdict"] == "slow"
    assert any("router" in f for f in r["findings"]) and any("Weak Wi-Fi" in f for f in r["findings"])


def test_net_check_offline(home, monkeypatch):
    monkeypatch.setattr(base, "sh", fake_sh({}))

    def boom(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(daily.socket, "getaddrinfo", boom)
    monkeypatch.setattr(daily.urllib.request, "urlopen", boom)
    r = daily.net_check()
    assert r["verdict"] == "offline"


class TestCli:
    def test_rejects_unknown_tool(self, capsys):
        assert tool_cli.main(["run", "{}"]) == 2

    def test_backup_restore_not_allowed_from_schedule(self, capsys):
        assert tool_cli.main(["backup_incremental", '{"action": "restore"}']) == 2
        assert "only backup" in capsys.readouterr().out

    def test_runs_and_notifies(self, home, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(base, "sh", lambda cmd, **k: sent.append(cmd) or base.ShellResult(cmd, 0, "", "", 0.0))
        monkeypatch.setattr(daily, "briefing", lambda: {"ok": True, "lines": ["Battery 50%"], "summary": "Battery 50%"})
        assert tool_cli.main(["briefing", "--notify", "Morning"]) == 0
        assert "Battery 50%" in capsys.readouterr().out
        assert any(c.startswith("termux-notification --title Morning") for c in sent)
