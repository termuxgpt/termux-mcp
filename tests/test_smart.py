import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import safety
from termux_mcp.smart import (SMART_TOOLS, MCP_DEFS, accuracy, cache, digest, fixgraph, intent,
                              reactor, run_smart_tool)


def call(tool_name, **params):
    out = run_smart_tool(tool_name, params)
    assert isinstance(out["text"], str)
    return out["digest"]


@pytest.fixture(autouse=True)
def fake_home(monkeypatch):
    home = tempfile.mkdtemp(prefix="smart-home-")
    monkeypatch.setattr(safety, "HOME", home)
    monkeypatch.setenv("HOME", home)
    cache.clear()
    yield home
    shutil.rmtree(home, ignore_errors=True)


class TestDigest:

    def test_error_codes_are_normalised(self):
        assert digest.classify("E: Unable to locate package foo")[0]["code"] == "E_PKG_NOT_FOUND"
        assert digest.classify("E: Unable to locate package foo")[0]["subject"] == "foo"
        assert digest.classify("ModuleNotFoundError: No module named 'requests'")[0]["subject"] == "requests"
        assert digest.classify("OSError: [Errno 98] Address already in use")[0]["code"] == "E_PORT_IN_USE"
        assert digest.classify("error: externally-managed-environment")[0]["code"] == "E_PIP_EXTERNAL"
        assert digest.classify("bash: htopx: command not found")[0]["code"] == "E_NOT_FOUND_CMD"
        assert digest.classify("fatal: not a git repository")[0]["code"] == "E_GIT_NOT_REPO"

    def test_unknown_failure_still_gets_a_code(self):
        assert digest.classify("something odd", 3)[0]["code"] == "E_EXIT"

    def test_pkg_install_parser(self):
        text = ("Setting up git (2.45.1) ...\nSetting up curl (8.8.0) ...\n"
                "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.")
        d = digest.make("pkg install -y git curl", 0, text)
        assert d["facts"]["packages"] == {"git": "2.45.1", "curl": "8.8.0"}
        assert "git 2.45.1" in d["summary"]

    def test_long_output_is_offloaded(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        d = digest.make("seq", 0, text)
        assert "out_ref" in d and "out" not in d
        assert d["out_bytes"] == len(text)
        assert len(json.dumps(d)) < 1500
        back = digest.read_output(d["out_ref"], grep=r"line 199\d$")
        assert back["shown"] == 10

    def test_ansi_is_stripped(self):
        assert digest.make("x", 0, "\x1b[31mred\x1b[0m")["out"] == "red"


class TestRunDigest:

    def test_success(self):
        d = call("run_digest", cmd="echo hello")
        assert d["ok"] and d["out"] == "hello" and d["exit"] == 0

    def test_failure_carries_code_and_fix(self):
        d = call("run_digest", cmd="definitely_not_a_command_xyz")
        assert not d["ok"]
        assert d["errors"][0]["code"] == "E_NOT_FOUND_CMD"
        assert d["fixes"] and d["fixes"][0]["id"] in ("cmd_install_pkg", "cmd_which_pkg")

    def test_lint_rewrites_braces(self, fake_home):
        d = call("run_digest", cmd=f"mkdir -p {fake_home}/a/{{b,c}}")
        assert os.path.isdir(os.path.join(fake_home, "a", "b"))
        assert os.path.isdir(os.path.join(fake_home, "a", "c"))
        assert "L_BRACE" in d["linted"]

    def test_timeout_never_hangs(self):
        start = time.time()
        d = run_smart_tool("run_digest", {"cmd": "sleep 30", "timeout": 1})["digest"]
        assert time.time() - start < 6
        assert d["errors"][0]["code"] == "E_TIMEOUT"

    def test_interactive_is_refused(self):
        assert call("run_digest", cmd="vim")["errors"][0]["code"] == "E_INTERACTIVE"

    def test_dangerous_is_blocked(self):
        d = call("run_digest", cmd="rm -rf /")
        assert not d["ok"] and d["errors"][0]["code"] in ("E_BLOCKED", "E_CONFIRM")

    def test_batch_runs_in_parallel(self):
        start = time.time()
        d = call("batch", calls=[{"cmd": "sleep 1; echo a"}, {"cmd": "sleep 1; echo b"},
                                 {"tool": "explain_cmd", "params": {"cmd": "ls -la"}}])
        assert time.time() - start < 2.5
        assert d["ok"] and [r["ok"] for r in d["results"]] == [True, True, True]

    def test_batch_refuses_writes(self):
        d = call("batch", calls=[{"tool": "file_edit", "params": {"path": "x", "content": "y"}}])
        assert not d["results"][0]["ok"]


class TestContext:

    def test_etag_roundtrip(self):
        first = call("context_pack")
        assert first["etag"] and "tools" in first
        again = call("context_pack", etag=first["etag"])
        assert again == {"ok": True, "unchanged": True, "etag": first["etag"]}

    def test_tool_search_finds_hidden_tools(self):
        d = call("tool_search", query="free a busy port")
        assert "port_free" in [t["name"] for t in d["tools"]]
        assert d["tools"][0]["inputSchema"]["type"] == "object"

    def test_smart_meta_reaches_unlisted_tools(self):
        listed = {t["name"] for t in MCP_DEFS}
        assert "explain_cmd" not in listed and "smart" in listed
        d = call("smart", tool="explain_cmd", params={"cmd": "rm -rf build"})
        assert "destructive" in d["effects"]

    def test_smart_meta_suggests_on_typo(self):
        d = call("smart", tool="port_kill")
        assert not d["ok"] and d["did_you_mean"]

    def test_cost_meter_counts_savings(self):
        call("run_digest", cmd="seq 1 5000")
        d = call("cost_meter")
        assert d["saved_tokens"] > 1000


class TestIntentTools:

    def test_file_edit_find_replace_and_undo_via_tx(self, fake_home):
        path = os.path.join(fake_home, "app.cfg")
        with open(path, "w") as f:
            f.write("port=8080\nmode=dev\n")
        tx = call("tx_begin", title="edit cfg")["tx"]
        d = call("file_edit", path=path, find="port=8080", replace="port=9090", task_id=tx)
        assert d["ok"] and d["verified"] and d["added"] == 1 and d["removed"] == 1
        assert "port=9090" in open(path).read()
        assert call("tx_status", tx=tx)["changes"] == 1
        assert not call("tx_rollback", tx=tx)["ok"]
        r = call("tx_rollback", tx=tx, confirmed=True)
        assert r["reverted"] == 1 and "port=8080" in open(path).read()

    def test_file_edit_no_match_is_an_error(self, fake_home):
        path = os.path.join(fake_home, "a.txt")
        open(path, "w").write("abc")
        assert call("file_edit", path=path, find="zzz", replace="y")["errors"][0]["code"] == "E_NO_MATCH"

    def test_port_free_reports_then_frees(self):
        srv = subprocess.Popen([sys.executable, "-c",
                                "import socket,time;s=socket.socket();s.setsockopt(1,2,1);"
                                "s.bind(('127.0.0.1',48123));s.listen();time.sleep(60)"])
        try:
            for _ in range(40):
                if socket.socket().connect_ex(("127.0.0.1", 48123)) == 0:
                    break
                time.sleep(0.1)
            d = call("port_free", port=48123)
            assert not d["ok"] and d["needs_confirmation"]
            d = call("port_free", port=48123, confirmed=True)
            if d["stopped"]:
                assert d["ok"] and d["free"]
        finally:
            srv.kill()

    def test_port_free_on_free_port(self):
        assert call("port_free", port=48124)["free"]

    def test_project_profile_python(self, fake_home):
        proj = os.path.join(fake_home, "bot")
        os.makedirs(proj)
        open(os.path.join(proj, "requirements.txt"), "w").write("requests\n")
        open(os.path.join(proj, "bot.py"), "w").write("print('hi')\n")
        d = call("project_profile", path=proj)
        assert d["lang"] == "python" and d["run"] == "python bot.py"
        assert "requirements.txt" in d["install"]

    def test_project_run_runs(self, fake_home):
        proj = os.path.join(fake_home, "hello")
        os.makedirs(proj)
        open(os.path.join(proj, "main.py"), "w").write("print('hello-run')\n")
        d = call("project_run", path=proj, install=False)
        assert d["ok"] and d["steps"][0]["out"] == "hello-run"

    def test_storage_clean_dry_run_then_apply(self, fake_home):
        pyc = os.path.join(fake_home, "proj", "__pycache__")
        os.makedirs(pyc)
        open(os.path.join(pyc, "x.pyc"), "wb").write(b"0" * 5000)
        d = call("storage_clean", categories=["pycache"])
        assert d["dry_run"] and d["categories"]["pycache"]["items"] == 1
        d = call("storage_clean", categories=["pycache"], dry_run=False, confirmed=True)
        assert d["ok"] and not os.path.exists(pyc)

    def test_git_sync(self, fake_home):
        if not shutil.which("git"):
            pytest.skip("git missing")
        remote = os.path.join(fake_home, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        work = os.path.join(fake_home, "work")
        subprocess.run(["git", "clone", "-q", remote, work], check=True, capture_output=True)
        subprocess.run("git -c user.email=a@b -c user.name=a commit -q --allow-empty -m one && git push -q origin HEAD",
                       shell=True, cwd=work, check=True, capture_output=True)
        d = call("git_sync", path=work)
        assert d["ok"] and d["behind"] == 0 and d["fetched"]

    def test_git_sync_not_a_repo(self, fake_home):
        assert call("git_sync", path=fake_home)["errors"][0]["code"] == "E_GIT_NOT_REPO"

    def test_service_ensure_and_stop(self, fake_home):
        d = call("service_ensure", name="web", cmd=f"{sys.executable} -m http.server 48125", port=48125, wait=10)
        try:
            assert d["ok"] and d["up"] and d["url"] == "http://localhost:48125"
            again = call("service_ensure", name="web")
            assert "already running" in again["summary"]
        finally:
            assert call("service_stop", name="web")["ok"]

    def test_pkg_ensure_validates_names(self):
        assert not call("pkg_ensure", names=["$(rm -rf ~)"])["ok"]


class TestRouting:

    @pytest.mark.parametrize("text,tool", [
        ("what's my battery doing?", "battery"),
        ("free port 8080", "port_free"),
        ("install git", ("pkg_ensure", "do")),
        ("how much storage is left", "run"),
        ("what did you change", "changes_list"),
        ("explain `tar -xzf a.tgz`", "explain_cmd"),
    ])
    def test_local_routes(self, text, tool):
        r = intent.route(text)
        assert r["candidates"][0]["tool"] in (tool if isinstance(tool, tuple) else (tool,)), r

    def test_port_is_extracted(self):
        assert intent.route("kill whatever is on port 3000")["candidates"][0]["params"] == {"port": 3000}

    def test_automation_goes_to_ai(self):
        assert intent.route("when battery is low notify me and stop the bot")["route"] != "local"

    def test_fix_lookup_and_confirm_gate(self):
        d = call("fix_lookup", error="E: Unable to locate package htopp")
        ids = [f["id"] for f in d["fixes"]]
        assert "pkg_search_name" in ids and "pkg_update_index" in ids
        gated = call("fix_apply", id="pkg_update_index", subject="htopp")
        assert gated["needs_confirmation"]

    def test_fixgraph_has_forty_fixes_with_valid_codes(self):
        codes = {c for c, _, _ in digest.ERROR_RULES}
        assert len(fixgraph.FIXES) >= 40
        assert all(f[1] in codes for f in fixgraph.FIXES)
        assert len({f[0] for f in fixgraph.FIXES}) == len(fixgraph.FIXES)


class TestAccuracy:

    def test_shell_lint(self):
        assert accuracy.shell_lint("sudo pkg install git")["cmd"] == "pkg install -y git"
        assert accuracy.shell_lint("cd /x && git log")["cmd"] == "git -C /x log"
        assert "L_INTERACTIVE" in [i["code"] for i in accuracy.shell_lint("nano a.txt")["issues"]]
        assert accuracy.shell_lint("ls {1..3}.txt")["cmd"] == "ls 1.txt 2.txt 3.txt"

    def test_plan_check_catches_problems(self, fake_home):
        d = call("plan_check", steps=[{"cmd": "nosuchtool --x"}, {"cmd": f"cat {fake_home}/missing.txt"},
                                      {"cmd": "echo fine"}])
        assert not d["ok"]
        assert d["steps"][0]["problems"][0]["code"] == "E_NOT_FOUND_CMD"
        assert d["steps"][1]["problems"][0]["code"] == "E_NO_FILE"
        assert "problems" not in d["steps"][2]

    def test_answer_check(self):
        good = call("answer_check", answer="Installed git 2.45.1.", evidence=['{"versions":{"git":"2.45.1"}}'],
                    actions=["pkg_ensure git"])
        assert good["grounded"]
        bad = call("answer_check", answer="Installed git 2.46.0 and freed 3.2 GB.",
                   evidence=['{"versions":{"git":"2.45.1"}}'], actions=["pkg_ensure git"])
        claims = {u["claim"] for u in bad["unsupported"]}
        assert "2.46.0" in claims and "freed" in claims


class TestUnique:

    def test_watch_file_changed_fires_once(self, fake_home):
        target = os.path.join(fake_home, "watched.txt")
        marker = os.path.join(fake_home, "fired.txt")
        open(target, "w").write("a")
        d = call("watch_add", when={"file_changed": target}, action={"cmd": f"echo x >> {marker}"})
        assert d["ok"]
        reactor.tick()
        time.sleep(0.05)
        os.utime(target, (time.time() + 5, time.time() + 5))
        assert reactor.tick() == [d["id"]]
        assert open(marker).read().strip() == "x"
        rules = call("watch_list")["rules"]
        assert rules[0]["fires"] == 1
        assert call("watch_remove", id=d["id"])["removed"] == 1

    def test_watch_rejects_unknown_condition(self):
        assert not call("watch_add", when={"moon": "full"}, action={"notify": "hi"})["ok"]

    def test_sandbox_shows_changes_then_applies(self, fake_home):
        proj = os.path.join(fake_home, "sb")
        os.makedirs(proj)
        open(os.path.join(proj, "a.txt"), "w").write("one\n")
        d = call("sandbox_run", cmd="echo two >> a.txt && touch b.txt && rm -f c.txt", path=proj)
        assert d["modified"] == ["a.txt"] and d["added"] == ["b.txt"]
        assert open(os.path.join(proj, "a.txt")).read() == "one\n"
        assert call("sandbox_apply", sandbox=d["sandbox"], confirmed=True)["applied"] == 2
        assert open(os.path.join(proj, "a.txt")).read() == "one\ntwo\n"

    def test_explain(self):
        d = call("explain_cmd", cmd="find . -name '*.log' | xargs rm -f > out.txt")
        names = [p.get("cmd") or p.get("op") for p in d["parts"]]
        assert names[:3] == ["find", "|", "xargs"]
        assert "writes a file" in d["effects"]

    def test_snapshot_env_writes_a_twin(self, fake_home):
        open(os.path.join(fake_home, ".bashrc"), "w").write("alias ll='ls -l'\n")
        d = call("snapshot_env", name="t1")
        assert d["ok"] and ".bashrc" in d["dotfiles"]
        plan = call("restore_env", file=d["file"])
        assert plan["dry_run"] and ".bashrc" in plan["plan"]["dotfiles"]

    def test_health_report(self):
        d = call("health_watch", action="run")
        assert "checks" in d and any(c["check"] == "storage" for c in d["checks"])
        assert call("health_watch", action="report")["cached"]

    def test_background_task(self):
        t = call("task_start", tool="run_digest", params={"cmd": "sleep 0.3; echo done"})
        for _ in range(40):
            s = call("task_status", task=t["task"])
            if s["status"] != "running":
                break
            time.sleep(0.1)
        assert s["status"] == "done" and s["result"]["out"] == "done"

    def test_graph_query(self, fake_home):
        os.makedirs(os.path.join(fake_home, "site"))
        open(os.path.join(fake_home, "site", "package.json"), "w").write("{}")
        call("graph_refresh", parts=["projects", "commands"])
        d = call("graph_query", q="my projects")
        assert d["facts"]["projects"][0]["lang"] == "node"
        d = call("graph_query", q="is sh installed")
        assert d["facts"]["installed"]


class TestTransports:

    def test_every_tool_name_dispatches(self):
        for name in SMART_TOOLS - {"smart"}:
            if name in ("notify_ask", "camera_scan", "screen_ocr", "project_run", "sandbox_run"):
                continue
            out = run_smart_tool(name, {})
            assert "text" in out and "E_UNKNOWN_TOOL" not in out["text"], name

    def test_websocket_routes_smart_tools(self):
        from termux_mcp import websocket

        class Sock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

        import threading
        sock = Sock()
        conn = {"cwd": os.getcwd(), "active_pid": None, "killed": threading.Event(),
                "lock": threading.Lock(), "send_lock": threading.Lock()}
        websocket._ws_execute_tool(sock, "run_digest", {"cmd": "echo ws"}, conn, "r1")
        blob = b"".join(sock.sent)
        assert b'"digest"' in blob and b"ws" in blob

    def test_native_mcp_returns_structured_content(self):
        from termux_mcp import mcp_core
        session = mcp_core.MCPSession("t", "test")
        out = mcp_core.call_tool(session, "run_digest", {"cmd": "echo mcp"})
        assert out["structuredContent"]["out"] == "mcp"
