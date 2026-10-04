import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import handler as handler_mod
from termux_mcp import mcp_bridge, websocket
from termux_mcp.utils import user_path

HOME = "/data/data/com.termux/files/home"


@pytest.fixture(autouse=True)
def home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


@pytest.mark.parametrize("given,expected", [
    ("~/joker", f"{HOME}/joker"),
    ("~", HOME),
    ("$HOME/joker", f"{HOME}/joker"),
    ("${HOME}/joker", f"{HOME}/joker"),
    ("  ~/joker  ", f"{HOME}/joker"),
    ("/sdcard/x", "/sdcard/x"),
    ("joker", "joker"),
    ("~other/x", "~other/x"),
])
def test_user_path(given, expected):
    assert user_path(given) == expected


def test_rest_mkdir_expands_tilde_and_reports_existing(monkeypatch):
    ran = []
    monkeypatch.setattr(handler_mod, "execute_streaming", lambda h, cmd, stdin_data=None: ran.append(cmd))
    vh = mcp_bridge.VirtualHandler()
    handler_mod.MCPHandler._handle_mkdir(vh, {"path": "~/joker"})
    [cmd] = ran
    assert f"{HOME}/joker" in cmd and "'~/joker'" not in cmd
    assert "Already exists" in cmd


def test_ws_mkdir_expands_tilde(monkeypatch):
    seen = []
    monkeypatch.setattr(websocket, "_ws_risk_gate", lambda *a, **k: False)
    monkeypatch.setattr(websocket, "_ws_run_process", lambda sock, cmd, conn, *a, **k: seen.append(cmd) or "")
    monkeypatch.setattr(websocket, "_ws_reply", lambda *a, **k: None)
    monkeypatch.setattr(websocket, "_send_frame", lambda *a, **k: None, raising=False)
    try:
        websocket._ws_execute_tool_inner(None, "mkdir", {"path": "~/joker"}, {}, "r1")
    except Exception:
        pass
    assert seen, "mkdir did not reach the shell"
    assert f"{HOME}/joker" in seen[0] and "'~/joker'" not in seen[0]
