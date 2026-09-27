import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import approval, auth, config, kernel, mcp_core, obs, policy, safety, websocket
from termux_mcp import handler as handler_mod
from termux_mcp.server import ThreadingHTTPServer
from termux_mcp.smart import run_smart_tool

SAFE = "echo parity"
WARN = "rm -r /tmp/parity-xyz-does-not-exist"
BLOCKED = "rm -rf /"


@pytest.fixture
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="hard-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    yield h
    shutil.rmtree(h, ignore_errors=True)


@pytest.fixture
def rest(monkeypatch, home):
    ran = []

    def fake_stream(h, cmd, stdin_data=None):
        ran.append(cmd)
        handler_mod.json_response(h, 200, {"ran": cmd})

    monkeypatch.setattr(handler_mod, "execute_streaming", fake_stream)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_mod.MCPHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], ran
    server.shutdown()
    server.server_close()


def rest_call(port, path, body, token=""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"raw": raw.decode(errors="replace")}


def rest_decision(port, cmd, confirmed=False, token=""):
    status, body = rest_call(port, "/run", {"cmd": cmd, "confirmed": confirmed}, token)
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "blocked" if body.get("blocked") else "denied"
    if body.get("status") == "confirmation_required":
        return "confirm"
    return "ok" if "ran" in body else f"? {status} {body}"


def ws_decision(monkeypatch, cmd, confirmed=False, principal=None):
    replies = []
    monkeypatch.setattr(websocket, "_ws_run_process", lambda sock, c, conn: "stub output")
    monkeypatch.setattr(websocket, "_ws_reply", lambda sock, conn, rid, data: replies.append(data))
    conn = {"cwd": safety.HOME, "send_lock": threading.Lock(), "killed": threading.Event(),
            "principal": principal}
    websocket._ws_execute_tool(None, "run", {"cmd": cmd, "confirmed": confirmed}, conn, 1)
    reply = replies[-1]
    if reply.get("is_error"):
        return "blocked" if reply.get("code") == "E_BLOCKED" else "denied"
    out = reply.get("output", "")
    if "confirmation_required" in out:
        return "confirm"
    return "ok" if out == "stub output" else f"? {reply}"


def mcp_decision(monkeypatch, cmd, confirmed=False, principal=None):
    monkeypatch.setattr(mcp_core, "_execute_command", lambda session, c: {"text": "stub output", "is_error": False})
    session = mcp_core.create_session("test")
    with kernel.acting_as(principal):
        res = mcp_core.invoke_tool(session, "run", {"cmd": cmd, "confirmed": confirmed})
    mcp_core.drop_session(session.sid)
    if res.get("is_error"):
        if res.get("code") == "E_BLOCKED":
            return "blocked"
        return "denied"
    if "confirmed: true" in res["text"]:
        return "confirm"
    return "ok" if res["text"] == "stub output" else f"? {res}"


def smart_decision(cmd, confirmed=False, principal=None):
    with kernel.acting_as(principal):
        d = run_smart_tool("run_digest", {"cmd": cmd, "confirmed": confirmed})["digest"]
    if d.get("ok"):
        return "ok"
    code = d["errors"][0]["code"]
    return {"E_BLOCKED": "blocked", "E_POLICY": "denied", "E_CAPABILITY": "denied",
            "E_CONFIRM": "confirm"}.get(code, f"? {code}")


class TestKernel:

    def test_termux_api_helpers_are_capped_everywhere(self, monkeypatch):
        monkeypatch.setattr(config, "COMMAND_TIMEOUT", 0)
        assert kernel.timeout_for("termux-battery-status") == 25
        assert kernel.timeout_for("FOO=1 termux-wifi-scaninfo") == 25
        assert kernel.timeout_for("ls") == 0
        assert kernel.timeout_for("ls", default=120) == 120
        assert kernel.timeout_for("termux-setup-storage") == 0

    def test_the_configured_timeout_applies_and_a_request_wins(self, monkeypatch):
        monkeypatch.setattr(config, "COMMAND_TIMEOUT", 600)
        assert kernel.timeout_for("pkg upgrade") == 600
        assert kernel.timeout_for("termux-battery-status") == 25
        assert kernel.timeout_for("pkg upgrade", requested=30) == 30
        monkeypatch.setattr(config, "COMMAND_TIMEOUT", 10)
        assert kernel.timeout_for("termux-battery-status") == 10

    def test_process_groups_never_use_preexec_fn(self):
        kw = kernel.popen_kwargs()
        assert "preexec_fn" not in kw
        if hasattr(os, "setsid"):
            assert kw == {"start_new_session": True}

    def test_no_transport_uses_preexec_fn_any_more(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "termux_mcp")
        for name in ("shell.py", "websocket.py", "mcp_core.py", os.path.join("smart", "base.py")):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                assert "preexec_fn" not in handle.read(), name

    def test_gate_order_policy_before_risk(self, home):
        policy.set_policy({"deny_paths": ["~/vault"]})
        assert kernel.gate("cat ~/vault/key")["status"] == "denied"
        assert kernel.gate(BLOCKED)["status"] == "blocked"
        assert kernel.gate(WARN)["status"] == "confirm"
        assert kernel.gate(WARN, confirmed=True)["status"] == "ok"
        assert kernel.gate(SAFE)["status"] == "ok"

    def test_one_approval_is_spent_exactly_once(self, home):
        approval.hold(WARN)
        assert kernel.gate(WARN)["status"] == "ok"
        assert kernel.gate(WARN)["status"] == "confirm"

    def test_watchdog_kills_the_whole_group(self):
        import subprocess
        proc = subprocess.Popen("sleep 30 & sleep 30; wait", shell=True, **kernel.popen_kwargs())
        fired = threading.Event()
        kernel.arm_watchdog(proc, 1, fired.set)
        start = time.time()
        proc.wait(timeout=10)
        assert fired.is_set() and time.time() - start < 8

    def test_prepare_snapshots_and_echoes(self, home):
        target = os.path.join(home, "f.txt")
        with open(target, "w") as handle:
            handle.write("old")
        cmd, snaps = kernel.prepare(f"echo new > {target}", "tx-1")
        assert snaps and cmd.startswith("echo 'snapshot: ")
        assert kernel.actions_for("tx-1")[-1]["cmd"] == f"echo new > {target}"


CASES = [(SAFE, False, "ok"), (WARN, False, "confirm"), (WARN, True, "ok"), (BLOCKED, False, "blocked"),
         (BLOCKED, True, "blocked")]


class TestTransportParity:

    @pytest.mark.parametrize("cmd,confirmed,expected", CASES)
    def test_same_decision_on_rest_ws_mcp_and_smart(self, rest, monkeypatch, cmd, confirmed, expected):
        port, _ = rest
        got = {"rest": rest_decision(port, cmd, confirmed),
               "ws": ws_decision(monkeypatch, cmd, confirmed),
               "mcp": mcp_decision(monkeypatch, cmd, confirmed)}
        if not (cmd == WARN and confirmed):
            got["smart"] = smart_decision(cmd, confirmed)
        assert set(got.values()) == {expected}, got

    def test_policy_denial_is_identical(self, rest, monkeypatch, home):
        policy.set_policy({"deny_paths": ["~/vault"]})
        cmd = "cat ~/vault/key"
        port, _ = rest
        got = {rest_decision(port, cmd), ws_decision(monkeypatch, cmd), mcp_decision(monkeypatch, cmd),
               smart_decision(cmd)}
        assert got == {"denied"}

    def test_network_off_is_identical(self, rest, monkeypatch, home):
        policy.set_policy({"network": False})
        cmd = "curl https://example.com"
        port, _ = rest
        got = {rest_decision(port, cmd), ws_decision(monkeypatch, cmd), mcp_decision(monkeypatch, cmd),
               smart_decision(cmd)}
        assert got == {"denied"}

    def test_a_device_approval_works_on_every_transport(self, rest, monkeypatch):
        port, _ = rest
        for decide in (lambda: rest_decision(port, WARN), lambda: ws_decision(monkeypatch, WARN),
                       lambda: mcp_decision(monkeypatch, WARN)):
            approval.hold(WARN)
            assert decide() == "ok"
            assert decide() == "confirm"

    def test_a_read_only_capability_is_identical(self, monkeypatch, home):
        p = auth.Principal("c-t", read_only=True)
        cmd = f"touch {home}/x"
        got = {ws_decision(monkeypatch, cmd, principal=p), mcp_decision(monkeypatch, cmd, principal=p),
               smart_decision(cmd, principal=p)}
        assert got == {"denied"}
        assert ws_decision(monkeypatch, SAFE, principal=p) == "ok"

    def test_smart_tools_answer_the_same_over_every_transport(self, rest, monkeypatch):
        port, _ = rest
        params = {"cmd": "ls -la | grep x"}
        direct = run_smart_tool("explain_cmd", params)["digest"]
        status, body = rest_call(port, "/explain_cmd", params)
        assert status == 200 and body["digest"] == direct
        replies = []
        monkeypatch.setattr(websocket, "_ws_reply", lambda sock, conn, rid, data: replies.append(data))
        websocket._ws_execute_tool(None, "explain_cmd", params,
                                   {"cwd": safety.HOME, "send_lock": threading.Lock()}, 3)
        assert replies[-1]["digest"] == direct
        session = mcp_core.create_session("test")
        res = mcp_core.call_tool(session, "smart", {"tool": "explain_cmd", "params": params})
        mcp_core.drop_session(session.sid)
        assert res["structuredContent"] == direct

    def test_every_watchdog_uses_the_kernel_timeout(self, monkeypatch, home):
        monkeypatch.setattr(config, "COMMAND_TIMEOUT", 1)
        from termux_mcp.mcp_bridge import VirtualHandler, decode_virtual
        from termux_mcp.shell import _run_process
        start = time.time()
        vh = VirtualHandler()
        _run_process(vh, "sleep 20")
        assert "Timed out after 1s" in decode_virtual(vh)["text"]
        frames = []
        monkeypatch.setattr(websocket, "_send_frame", lambda sock, conn, data, *a: frames.append(data))
        out = websocket._ws_run_process(None, "sleep 20", {"cwd": home, "killed": threading.Event(),
                                                             "send_lock": threading.Lock()})
        assert "Timed out after 1s" in out
        session = mcp_core.create_session("test")
        res = mcp_core._execute_command(session, "sleep 20")
        mcp_core.drop_session(session.sid)
        assert "Timed out after 1s" in res["text"]
        d = run_smart_tool("run_digest", {"cmd": "sleep 20"})["digest"]
        assert d["errors"][0]["code"] == "E_TIMEOUT"
        assert time.time() - start < 20


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv("TERMUX_MCP_AUTH", "on")
    monkeypatch.delenv("TERMUX_MCP_AUTH_TOKEN", raising=False)
    folder = tempfile.mkdtemp(prefix="auth-")
    monkeypatch.setenv("TERMUX_MCP_CONFIG_DIR", folder)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


class TestAuth:

    def test_a_token_is_generated_privately_on_first_use(self, auth_on):
        token = auth.master_token()
        assert len(token) >= 32 and auth.required()
        path = os.path.join(auth_on, "token")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        assert auth.master_token() == token

    def test_off_switch_and_env_override(self, auth_on, monkeypatch):
        monkeypatch.setenv("TERMUX_MCP_AUTH", "off")
        assert auth.master_token() == "" and auth.authenticate("") == (True, None)
        monkeypatch.setenv("TERMUX_MCP_AUTH_TOKEN", "x" * 20)
        assert auth.master_token() == "x" * 20

    def test_rotation_takes_effect_without_restart(self, auth_on):
        old = auth.master_token()
        new = auth.rotate()
        assert new != old and auth.master_token() == new
        assert auth.authenticate(old)[0] is False and auth.authenticate(new)[0] is True

    def test_rest_requires_the_token_and_ping_stays_open(self, auth_on, rest):
        port, _ = rest
        assert rest_decision(port, SAFE) == "unauthorized"
        assert rest_decision(port, SAFE, token="wrong") == "unauthorized"
        assert rest_decision(port, SAFE, token=auth.master_token()) == "ok"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/ping")
        assert conn.getresponse().status == 200
        conn.close()

    def test_websocket_and_native_mcp_share_the_token(self, auth_on, monkeypatch):
        token = auth.master_token()
        assert websocket._ws_authenticated("", "/ws") is False
        assert websocket._ws_authenticated(f"Authorization: Bearer {token}", "/ws") is True
        assert websocket._ws_authenticated("", f"/ws?token={token}") is True
        from termux_mcp import mcp_config, mcp_transport_http
        monkeypatch.delenv("TERMUX_NATIVE_MCP_AUTH_TOKEN", raising=False)
        assert mcp_config.native_auth_token() == token and mcp_config.require_auth()
        assert mcp_transport_http._auth_ok(token) and not mcp_transport_http._auth_ok("nope")

    def test_cli_token(self, auth_on, capsys):
        assert auth.cli(["token"]) == 0
        assert auth.master_token() in capsys.readouterr().out


class TestMetrics:

    def test_calls_are_counted_per_tool_with_codes(self, rest, home):
        obs.reset()
        port, _ = rest
        rest_call(port, "/explain_cmd", {"cmd": "ls"})
        rest_call(port, "/run_digest", {"cmd": "definitely-not-a-command-xyz"})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/metrics")
        data = json.loads(conn.getresponse().read())
        conn.close()
        assert data["tools"]["explain_cmd"]["count"] == 1
        assert data["tools"]["run_digest"]["errors"] == 1
        assert data["error_codes"]
        assert data["tools"]["explain_cmd"]["transports"] == {"rest": 1}

    def test_events_are_structured_jsonl(self, home):
        obs.record("ws", "demo", 12.5, False, "E_DEMO")
        with open(obs._log_path(), encoding="utf-8") as handle:
            last = json.loads(handle.read().strip().splitlines()[-1])
        assert last["tool"] == "demo" and last["code"] == "E_DEMO" and last["transport"] == "ws"

    def test_percentiles(self):
        obs.reset()
        for ms in range(1, 101):
            obs.record("mcp", "p", ms, True)
        m = obs.metrics()["tools"]["p"]
        assert m["p50_ms"] in (50, 51) and m["p95_ms"] >= 94


def test_themes_are_inside_the_package():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "termux_mcp", "themes")
    assert len(os.listdir(os.path.join(root, "dark"))) >= 40
    assert len(os.listdir(os.path.join(root, "light"))) >= 30
