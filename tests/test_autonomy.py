import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import approval, auth, kernel, mcp_core, policy, safety, websocket
from termux_mcp.smart import cache, learn, run_smart_tool


def call(tool_name, **params):
    return run_smart_tool(tool_name, params)["digest"]


@pytest.fixture(autouse=True)
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="auto-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    yield h
    shutil.rmtree(h, ignore_errors=True)


class TestGoalRun:

    def test_success_is_verified_committed_and_cached(self, home):
        f = os.path.join(home, "goal.txt")
        r = call("goal_run", goal="write goal file", steps=[f"echo ok > {f}"], verify=[f"grep -q ok {f}"])
        assert r["ok"] and r["status"] == "done" and r["verified"][0]["ok"]
        assert call("tx_status", tx=r["tx"])["status"] == "committed"
        assert learn.plan_cache_get("write goal file")["hit"]

    def test_failure_rolls_everything_back_and_returns_a_decision(self, home):
        f = os.path.join(home, "keep.txt")
        with open(f, "w") as handle:
            handle.write("original")
        r = call("goal_run", goal="break it", steps=[f"echo changed > {f}", "exit 3"])
        assert r["status"] == "decision" and r["decision"] == "step_failed" and r["failed_step"] == 2
        assert r["rolled_back"] >= 1 and open(f).read() == "original"

    def test_verify_failure_rolls_back(self, home):
        f = os.path.join(home, "v.txt")
        r = call("goal_run", steps=[f"echo a > {f}"], verify=["false"])
        assert r["decision"] == "verify_failed" and not os.path.exists(f)

    def test_a_known_safe_fix_is_tried_once(self, home, monkeypatch):
        from termux_mcp.smart import fixgraph
        calls = []
        monkeypatch.setattr(fixgraph, "lookup", lambda **kw: {"fixes": [
            {"id": "demo", "risk": "safe", "desc": "demo", "steps": ["true"]}]})
        monkeypatch.setattr(fixgraph, "apply", lambda *a, **kw: calls.append(a) or {"ok": True, "summary": "x"})
        flag = os.path.join(home, "flag")
        step = f"test -f {flag} || (touch {flag}; exit 1)"
        r = call("goal_run", steps=[step])
        assert r["ok"] and r["fixes_used"] == 1 and len(calls) == 1

    def test_blocked_and_risky_plans_stop_before_running(self):
        assert call("goal_run", steps=["rm -rf /"])["decision"] == "blocked"
        r = call("goal_run", steps=["rm -r /tmp/goal-xyz-none"])
        assert r["decision"] == "confirm" and r["needs_confirmation"]

    def test_budget(self):
        r = call("goal_run", steps=["true"] * 5, budget={"steps": 3})
        assert r["decision"] == "over_budget"

    def test_no_plan_asks_for_one_and_dry_run_runs_nothing(self, home):
        assert call("goal_run", goal="something new")["decision"] == "need_plan"
        f = os.path.join(home, "dry.txt")
        r = call("goal_run", steps=[f"echo x > {f}"], dry_run=True)
        assert r["dry_run"] and not os.path.exists(f)

    def test_goal_uses_the_plan_cache(self, home):
        f = os.path.join(home, "cached.txt")
        learn.plan_cache_put("make the cached file", [f"echo c > {f}"])
        r = call("goal_run", goal="make the cached file")
        assert r["ok"] and r["source"] == "plan_cache" and os.path.exists(f)


class TestPolicy:

    def test_validation(self):
        assert call("policy_set", policy={"bogus": 1}, confirmed=True)["errors"][0]["code"] == "E_ARGS"
        assert call("policy_set", policy={"pip": "maybe"}, confirmed=True)["errors"][0]["code"] == "E_ARGS"
        assert call("policy_set", policy={"network": False})["needs_confirmation"]

    def test_tightening_is_free_loosening_needs_device_approval(self):
        assert call("policy_set", policy={"network": False}, confirmed=True)["ok"]
        denied = call("policy_set", policy={"network": True}, confirmed=True)
        assert denied["errors"][0]["code"] == "E_APPROVAL"
        approval.hold("policy_set")
        assert call("policy_set", policy={"network": True}, confirmed=True)["ok"]
        assert policy.get()["network"] is True

    def test_deny_paths_cover_shell_and_tools(self, home):
        secret = os.path.join(home, "vault")
        os.makedirs(secret)
        policy.set_policy({"deny_paths": ["~/vault"]})
        assert call("run_digest", cmd="ls ~/vault")["errors"][0]["code"] == "E_POLICY"
        assert call("run_digest", cmd=f"cat {secret}/k")["errors"][0]["code"] == "E_POLICY"
        assert call("file_edit", path=f"{secret}/k", content="x")["errors"][0]["code"] == "E_POLICY"
        assert call("file_find", path="~/vault")["errors"][0]["code"] == "E_POLICY"
        assert call("run_digest", cmd="echo fine")["ok"]

    def test_allow_paths_limit_writes(self, home):
        os.makedirs(os.path.join(home, "projects"))
        policy.set_policy({"allow_paths": ["~/projects"]})
        assert call("run_digest", cmd=f"echo x > {home}/projects/a")["ok"]
        assert call("run_digest", cmd=f"echo x > {home}/b")["errors"][0]["code"] == "E_POLICY"
        assert call("run_digest", cmd=f"rm {home}/c")["errors"][0]["code"] == "E_POLICY"
        assert call("run_digest", cmd=f"cat {home}/b; echo read-is-fine")["ok"] is not None

    def test_deny_tools_and_pip_venv_only(self, home):
        policy.set_policy({"deny_tools": ["web_fetch"], "pip": "venv_only"})
        assert call("web_fetch", url="https://example.com")["errors"][0]["code"] == "E_POLICY"
        assert call("run_digest", cmd="pip install requests")["errors"][0]["code"] == "E_POLICY"
        d = kernel.gate(".venv/bin/pip install requests")
        assert d["status"] == "ok"

    def test_policy_is_enforced_in_the_kernel_for_every_endpoint(self, home):
        from termux_mcp.mcp_bridge import VirtualHandler
        from termux_mcp.shell import execute_streaming
        policy.set_policy({"network": False})
        vh = VirtualHandler()
        execute_streaming(vh, "curl -s https://example.com")
        assert vh.status == 403

    def test_policy_get(self):
        assert call("policy_get")["summary"].startswith("default")
        policy.set_policy({"network": False})
        assert call("policy_get")["active"] == {"network": False}


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv("TERMUX_MCP_AUTH", "on")
    monkeypatch.delenv("TERMUX_MCP_AUTH_TOKEN", raising=False)
    folder = tempfile.mkdtemp(prefix="caps-")
    monkeypatch.setenv("TERMUX_MCP_CONFIG_DIR", folder)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


class TestCapabilities:

    def test_issue_needs_confirmation_and_the_token_is_hashed(self, auth_on):
        assert call("cap_issue", tools=["file_find"])["needs_confirmation"]
        r = call("cap_issue", tools=["file_find"], read_only=True, ttl_min=5, confirmed=True)
        with open(os.path.join(auth_on, auth.CAPS_FILE)) as handle:
            stored = handle.read()
        assert r["token"].startswith("cap_") and r["token"] not in stored
        ok, principal = auth.authenticate(r["token"])
        assert ok and principal.read_only and principal.tools == {"file_find"}

    def test_scopes_are_enforced(self, auth_on, home):
        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        r = auth.cap_issue(tools=["file_find", "run_digest", "db_query", "batch"], paths=[proj], read_only=True)
        _, p = auth.authenticate(r["token"])
        with kernel.acting_as(p):
            assert call("file_find", path=proj)["ok"]
            assert call("storage_clean")["errors"][0]["code"] == "E_CAPABILITY"
            assert call("file_find", path=home)["errors"][0]["code"] == "E_CAPABILITY"
            assert call("run_digest", cmd=f"ls {proj}")["ok"]
            assert call("run_digest", cmd=f"touch {proj}/x")["errors"][0]["code"] == "E_CAPABILITY"
            assert call("run_digest", cmd="cat /etc/hostname")["errors"][0]["code"] == "E_CAPABILITY"
            assert call("smart", tool="storage_clean", params={})["errors"][0]["code"] == "E_CAPABILITY"
            assert call("cap_issue", confirmed=True)["errors"][0]["code"] == "E_CAPABILITY"
            assert call("batch", calls=[{"tool": "timeline"}])["results"][0]["errors"][0]["code"] == "E_CAPABILITY"

    def test_expiry_and_revoke(self, auth_on):
        r = auth.cap_issue(tools=["timeline"], ttl_min=1)
        assert auth.authenticate(r["token"])[0]
        call("cap_revoke", id=r["id"])
        assert auth.authenticate(r["token"])[0] is False
        r2 = auth.cap_issue(tools=["timeline"])
        caps = auth._load_caps()
        for entry in caps.values():
            entry["expires"] = 1
        auth._save_caps(caps)
        assert auth.authenticate(r2["token"])[0] is False

    def test_capability_scope_on_websocket_and_pty(self, auth_on, monkeypatch):
        r = auth.cap_issue(tools=["explain_cmd"], read_only=True)
        headers = f"Authorization: Bearer {r['token']}"
        allowed, principal = websocket._ws_auth(headers, "/ws")
        assert allowed and principal is not None
        replies = []
        monkeypatch.setattr(websocket, "_ws_reply", lambda sock, conn, rid, data: replies.append(data))
        conn = {"cwd": safety.HOME, "send_lock": threading.Lock(), "principal": principal}
        websocket._ws_execute_tool(None, "storage_clean", {}, conn, 1)
        assert replies[-1]["code"] == "E_CAPABILITY"
        websocket._ws_execute_tool(None, "explain_cmd", {"cmd": "ls"}, conn, 2)
        assert replies[-1]["digest"]["ok"]

    def test_capability_scope_on_native_mcp(self, auth_on):
        r = auth.cap_issue(tools=["timeline"])
        _, p = auth.authenticate(r["token"])
        session = mcp_core.create_session("test")
        with kernel.acting_as(p):
            res = mcp_core.invoke_tool(session, "run", {"cmd": "echo hi"})
        mcp_core.drop_session(session.sid)
        assert res["is_error"] and "does not allow" in res["text"]


WRITE_CALLS = [
    ("run_digest", {"cmd": "echo x > /tmp/dry-x"}), ("pkg_ensure", {"names": ["git"]}),
    ("service_ensure", {"name": "s", "cmd": "sleep 1"}), ("service_stop", {"name": "s"}),
    ("git_sync", {}), ("fix_apply", {"id": "pkg_update_index"}), ("tx_rollback", {"tx": "tx-none"}),
    ("watch_add", {"when": {"every_min": 5}, "action": {"notify": "x"}}), ("watch_remove", {"id": "w"}),
    ("sandbox_apply", {"sandbox": "sb"}), ("cron_ensure", {"name": "n", "spec": "* * * * *", "cmd": "true"}),
    ("tunnel", {"port": 8080}), ("venv_ensure", {"path": "/tmp/dry-venv"}), ("undo_task", {"task": "tx-none"}),
    ("media_convert", {"input": "a", "output": "b"}), ("ssh_run", {"host": "h", "cmd": "ls"}),
    ("backup_incremental", {}), ("plan_replay", {"steps": ["echo a"]}), ("learn_save", {"draft_id": "d"}),
]


class TestDryRun:

    @pytest.mark.parametrize("tool,params", WRITE_CALLS, ids=[c[0] for c in WRITE_CALLS])
    def test_every_write_tool_supports_dry_run(self, tool, params, home):
        before = sorted(os.listdir(home))
        r = call(tool, dry_run=True, **params)
        assert r.get("dry_run") is True, r
        assert "summary" in r
        assert sorted(os.listdir(home)) == before or set(os.listdir(home)) <= set(before) | {"termuxGPT"}
        assert not os.path.exists("/tmp/dry-x") and not os.path.exists("/tmp/dry-venv")

    def test_the_dry_run_reports_the_kernel_decision(self):
        assert call("run_digest", cmd="rm -rf /", dry_run=True)["decision"] == "blocked"
        r = call("run_digest", cmd="rm -r /tmp/dry-y", dry_run=True)
        assert r["decision"] == "confirm" and r["writes"] == ["/tmp/dry-y"]

    def test_unknown_scripts_run_in_a_sandbox_first(self, home):
        work = os.path.join(home, "w")
        os.makedirs(work)
        script = os.path.join(work, "go.sh")
        with open(script, "w") as handle:
            handle.write("echo made > out.txt\n")
        policy.set_policy({"sandbox_unknown_scripts": True})
        first = call("run_digest", cmd=f"bash {script}", cwd=work)
        assert first["errors"][0]["code"] == "E_SANDBOXED" and not os.path.exists(os.path.join(work, "out.txt"))
        real = call("run_digest", cmd=f"bash {script}", cwd=work, confirmed=True)
        assert real["ok"] and os.path.exists(os.path.join(work, "out.txt"))
        again = call("run_digest", cmd=f"bash {script}", cwd=work)
        assert again["ok"]
        with open(script, "a") as handle:
            handle.write("echo changed\n")
        assert call("run_digest", cmd=f"bash {script}", cwd=work)["errors"][0]["code"] == "E_SANDBOXED"


class TestTimeline:

    def test_tasks_are_grouped_and_any_one_can_be_undone(self, home):
        a, b = os.path.join(home, "a.txt"), os.path.join(home, "b.txt")
        t1 = call("tx_begin", title="first")["tx"]
        call("run_digest", cmd=f"echo 1 > {a}", task_id=t1)
        t2 = call("tx_begin", title="second")["tx"]
        call("run_digest", cmd=f"echo 2 > {b}", task_id=t2)
        rows = call("timeline")["tasks"]
        assert [r["task"] for r in rows[:2]] == [t2, t1] and rows[1]["title"] == "first"
        preview = call("undo_task", task=t1)
        assert preview["needs_confirmation"] and preview["files"] == [a]
        done = call("undo_task", task=t1, confirmed=True)
        assert done["reverted"] == 1 and not os.path.exists(a) and os.path.exists(b)

    def test_undo_warns_about_later_edits_of_the_same_file(self, home):
        f = os.path.join(home, "shared.txt")
        with open(f, "w") as handle:
            handle.write("v0")
        t1 = call("tx_begin")["tx"]
        call("run_digest", cmd=f"echo v1 > {f}", task_id=t1)
        t2 = call("tx_begin")["tx"]
        call("run_digest", cmd=f"echo v2 > {f}", task_id=t2)
        preview = call("undo_task", task=t1)
        assert preview["conflicts"] == [f]
        call("undo_task", task=t1, confirmed=True)
        assert open(f).read() == "v0"

    def test_unknown_task(self):
        assert call("undo_task", task="tx-nope")["errors"][0]["code"] == "E_NO_TASK"
