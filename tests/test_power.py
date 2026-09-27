import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from termux_mcp import policy, safety
from termux_mcp.smart import base, cache, power, reactor, run_smart_tool


def call(tool_name, **params):
    return run_smart_tool(tool_name, params)["digest"]


@pytest.fixture(autouse=True)
def home(monkeypatch):
    h = tempfile.mkdtemp(prefix="power-home-")
    monkeypatch.setattr(safety, "HOME", h)
    monkeypatch.setenv("HOME", h)
    cache.clear()
    yield h
    shutil.rmtree(h, ignore_errors=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestEnvironments:

    def test_venv_is_created_once_and_requirements_are_cached(self, home):
        proj = os.path.join(home, "app")
        write(os.path.join(proj, "requirements.txt"), "# nothing to install\n")
        first = call("venv_ensure", path=proj, python=sys.executable)
        assert first["ok"] and first["created"] and first["installed"], first
        assert os.path.exists(first["python"])
        second = call("venv_ensure", path=proj, python=sys.executable)
        assert second["ok"] and not second["created"] and second["cached"]
        write(os.path.join(proj, "requirements.txt"), "# changed\n")
        third = call("venv_ensure", path=proj, python=sys.executable)
        assert third["installed"] and not third["cached"]

    def test_venv_arguments(self):
        assert call("venv_ensure", path="")["errors"][0]["code"] == "E_ARGS"
        assert call("venv_ensure", path="/x", python="py; rm -rf /")["errors"][0]["code"] == "E_ARGS"

    @pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
    def test_node_version_and_deps_are_cached(self, home, monkeypatch):
        proj = os.path.join(home, "web")
        write(os.path.join(proj, "package.json"), '{"name": "t", "version": "1.0.0"}')
        r = call("node_ensure")
        assert r["ok"] and r["node"].startswith("v")
        major = r["node"].lstrip("v").split(".")[0]
        assert call("node_ensure", version=major)["ok"]
        calls = []
        real = power._run
        monkeypatch.setattr(power, "_run", lambda cmd, **kw: (calls.append(cmd), real("true"))[1])
        assert call("node_ensure", path=proj)["deps"] == "installed"
        assert call("node_ensure", path=proj)["deps"] == "unchanged"
        assert calls == ["npm install --no-audit --no-fund"]

    def test_proot_needs_confirmation_and_checks_installed(self, monkeypatch, home):
        monkeypatch.setattr(power.base, "which", lambda name: True)
        monkeypatch.setenv("PREFIX", home)
        r = call("proot_ensure", distro="debian")
        assert r["errors"][0]["code"] == "E_CONFIRM"
        os.makedirs(os.path.join(power._rootfs("debian"), "etc"))
        assert call("proot_ensure", distro="debian")["installed"]
        assert call("proot_ensure", distro="bad name;")["errors"][0]["code"] == "E_ARGS"
        assert call("proot_run", distro="ubuntu", cmd="ls")["errors"][0]["code"] == "E_NOT_INSTALLED"


@pytest.fixture
def fake_crontab(home, monkeypatch):
    binr = os.path.join(home, "bin")
    tab = os.path.join(home, "tab")
    write(os.path.join(binr, "crontab"), f"""#!/bin/sh
if [ "$1" = "-l" ]; then [ -f {tab} ] && cat {tab} || exit 1; else cat > {tab}; fi
""")
    write(os.path.join(binr, "crond"), "#!/bin/sh\ntouch " + os.path.join(home, "crond-started") + "\n")
    write(os.path.join(binr, "pgrep"), "#!/bin/sh\n[ -f " + os.path.join(home, "crond-started") + " ]\n")
    for f in os.listdir(binr):
        os.chmod(os.path.join(binr, f), 0o755)
    monkeypatch.setenv("PATH", binr + os.pathsep + os.environ["PATH"])
    return tab


class TestCron:

    def test_ensure_is_idempotent_logged_and_verified(self, fake_crontab):
        r = call("cron_ensure", name="ping", spec="*/15 * * * *", cmd="echo hi")
        assert r["ok"] and r["changed"] and r["verified"] and r["running"]
        assert r["line"].endswith("# tmcp:ping") and ">> " in r["line"]
        again = call("cron_ensure", name="ping", spec="*/15 * * * *", cmd="echo hi")
        assert again["ok"] and again["changed"] is False
        call("cron_ensure", name="ping", spec="0 9 * * *", cmd="echo hi")
        assert open(fake_crontab).read().count("tmcp:ping") == 1
        jobs = call("cron_ensure", action="list")["jobs"]
        assert [j["name"] for j in jobs] == ["ping"]
        assert call("cron_ensure", name="ping", action="remove")["ok"]
        assert "tmcp:ping" not in open(fake_crontab).read()

    def test_bad_specs_and_risky_commands(self, fake_crontab):
        assert call("cron_ensure", name="x", spec="every day", cmd="true")["errors"][0]["code"] == "E_ARGS"
        assert call("cron_ensure", name="x", spec="@daily", cmd="rm -rf /")["errors"][0]["code"] == "E_BLOCKED"
        assert call("cron_ensure", name="x", spec="@daily", cmd="rm -r ~/old")["errors"][0]["code"] == "E_CONFIRM"
        assert call("cron_ensure", name="bad name", spec="@daily", cmd="true")["errors"][0]["code"] == "E_ARGS"


class TestTunnel:

    def test_port_must_be_up_and_exposure_confirmed(self):
        assert call("tunnel", port=1)["errors"][0]["code"] == "E_PORT_DOWN"
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            r = call("tunnel", port=server.server_address[1])
            assert r["errors"][0]["code"] == "E_CONFIRM" and r["needs_confirmation"]
        finally:
            server.shutdown()
            server.server_close()

    def test_list_and_stop_are_safe(self):
        assert call("tunnel", action="list")["tunnels"] == []
        assert call("tunnel", action="stop")["stopped"] == 0

    def test_network_policy_blocks_tunnels(self, monkeypatch):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setattr(power.base, "which", lambda name: name == "cloudflared" or shutil.which(name))
        try:
            policy.set_policy({"network": False})
            r = call("tunnel", port=server.server_address[1], confirmed=True)
            assert r["errors"][0]["code"] == "E_POLICY"
        finally:
            server.shutdown()
            server.server_close()


PAGE = """<html><head><title>Demo &amp; Test</title><script>var x=1;</script><style>p{}</style></head>
<body><nav>Home | About</nav><article><h1>Big news</h1><p>First paragraph of the article that is long
enough to count as the main content of this page, with several words.</p><p>Second one.</p></article>
<div class="price">42 EUR</div><div id="foot">footer</div><a href="/x">x</a><a href="/y">y</a></body></html>"""


@pytest.fixture
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/json":
                body, ctype = json.dumps({"a": 1, "b": [1, 2]}).encode(), "application/json"
            elif self.path == "/long":
                body = ("<html><body><main>" + "<p>word " * 800 + "</main></body></html>").encode()
                ctype = "text/html"
            elif self.path == "/missing":
                self.send_response(404)
                self.end_headers()
                return
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class TestWebFetch:

    def test_readable_text_prefers_the_article(self, site):
        r = call("web_fetch", url=site + "/")
        assert r["ok"] and r["title"] == "Demo & Test" and r["links"] == 2
        assert "Big news" in r["text"] and "var x" not in r["text"] and "Home | About" not in r["text"]

    def test_selectors(self, site):
        assert call("web_fetch", url=site + "/", selector=".price")["text"] == "42 EUR"
        assert call("web_fetch", url=site + "/", selector="#foot")["text"] == "footer"
        assert call("web_fetch", url=site + "/", selector="div.nothing")["errors"][0]["code"] == "E_NO_MATCH"
        assert call("web_fetch", url=site + "/", selector="a > b")["errors"][0]["code"] == "E_ARGS"

    def test_json_errors_and_long_pages(self, site):
        r = call("web_fetch", url=site + "/json")
        assert r["json_keys"] == ["a", "b"]
        assert call("web_fetch", url=site + "/missing")["errors"][0]["code"] == "E_HTTP"
        assert call("web_fetch", url="file:///etc/passwd")["errors"][0]["code"] == "E_ARGS"
        short = call("web_fetch", url=site + "/long", max_chars=200)
        assert "out_ref" in short and call("out_read", ref=short["out_ref"])["ok"]

    def test_network_policy(self, site):
        policy.set_policy({"network": False})
        assert call("web_fetch", url=site + "/")["errors"][0]["code"] == "E_POLICY"


class TestDbQuery:

    def test_sqlite_read_only_by_default(self, home):
        db = os.path.join(home, "a.db")
        con = sqlite3.connect(db)
        con.executescript("create table u(id integer, name text); insert into u values (1,'ann'),(2,'bob');")
        con.commit()
        con.close()
        r = call("db_query", path=db, sql="select name from u order by id")
        assert r["rows"] == [["ann"], ["bob"]] and "ann" in r["table"]
        assert call("db_query", path=db, sql="delete from u")["errors"][0]["code"] == "E_READ_ONLY"
        assert call("db_query", path=db, sql="delete from u", write=True)["errors"][0]["code"] == "E_CONFIRM"
        w = call("db_query", path=db, sql="delete from u where id=1", write=True, confirmed=True)
        assert w["ok"] and w["changed"] == 1
        assert call("db_query", path=db, sql="select count(*) from u")["rows"] == [[1]]

    def test_csv_as_a_table(self, home):
        f = write(os.path.join(home, "sales.csv"), "Region,Total Sales\nnorth,10\nsouth,32\nnorth,5\n")
        r = call("db_query", path=f, sql="select Region, sum(Total_Sales) s from t group by Region order by s desc")
        assert r["rows"] == [["south", 32], ["north", 15]]
        assert call("db_query", path=f, sql="select count(*) from sales")["rows"] == [[3]]

    def test_limits_errors_and_big_results(self, home):
        db = os.path.join(home, "b.db")
        con = sqlite3.connect(db)
        con.execute("create table n(i integer, pad text)")
        con.executemany("insert into n values (?, ?)", [(i, "x" * 30) for i in range(500)])
        con.commit()
        con.close()
        r = call("db_query", path=db, sql="select * from n", limit=100)
        assert r["count"] == 100 and r["more"] and r["out_ref"]
        assert call("db_query", path=db, sql="select * from nope")["errors"][0]["code"] == "E_SQL"
        assert call("db_query", path=os.path.join(home, "x.db"), sql="select 1")["errors"][0]["code"] == "E_NO_FILE"


class TestLogWatch:

    def test_scan(self, home):
        log = write(os.path.join(home, "app.log"), "ok\nERROR disk\nok\nerror net\n")
        r = call("log_watch", path=log, pattern="error")
        assert r["matches"] == 2 and r["last"][-1] == "error net"

    def test_watch_fires_only_on_new_lines(self, home, monkeypatch):
        log = write(os.path.join(home, "app.log"), "ERROR old\n")
        fired = []
        monkeypatch.setattr(reactor, "_fire", lambda rule: fired.append(rule.get("_last_match")) or "notified")
        monkeypatch.setattr(reactor, "ensure_running", lambda: None)
        r = call("log_watch", path=log, pattern="ERROR", action="watch")
        assert r["ok"]
        reactor.tick()
        assert fired == []
        with open(log, "a") as handle:
            handle.write("fine\nERROR new one\n")
        reactor.tick()
        assert fired == ["ERROR new one"]
        reactor.tick()
        assert fired == ["ERROR new one"]
        with open(log, "a") as handle:
            handle.write("ERROR again\n")
        reactor.tick()
        assert fired[-1] == "ERROR again"
        assert call("log_watch", action="stop", id=r["id"])["removed"] == 1

    def test_bad_pattern(self, home):
        log = write(os.path.join(home, "a.log"), "x")
        assert call("log_watch", path=log, pattern="(")["errors"][0]["code"] == "E_ARGS"


class TestFileFind:

    def test_name_content_filters_and_index(self, home):
        write(os.path.join(home, "docs", "Report.pdf"), "x" * 10)
        write(os.path.join(home, "docs", "notes.md"), "remember the milk")
        write(os.path.join(home, "big.bin"), "0" * 3_000_000)
        write(os.path.join(home, "node_modules", "junk", "notes.md"), "milk")
        r = call("file_find", query="notes", path=home)
        assert [os.path.basename(f["path"]) for f in r["files"]] == ["notes.md"]
        assert call("file_find", query="*.pdf", path=home)["count"] == 1
        c = call("file_find", query="MILK", path=home, kind="content")
        assert [os.path.basename(f["path"]) for f in c["files"]] == ["notes.md"]
        big = call("file_find", path=home, larger_than_mb=1)
        assert [os.path.basename(f["path"]) for f in big["files"]] == ["big.bin"]
        assert call("file_find", query="report", path=home, ext="pdf")["count"] == 1
        assert call("file_find", query="notes", path=home)["index_cached"] is True
        write(os.path.join(home, "docs", "notes2.md"), "")
        assert call("file_find", query="notes", path=home, refresh=True)["count"] == 2


class TestBackup:

    def test_incremental_verify_restore(self, home):
        src = os.path.join(home, "data")
        dest = os.path.join(home, "bk")
        write(os.path.join(src, "a.txt"), "alpha")
        write(os.path.join(src, "sub", "b.txt"), "beta")
        first = call("backup_incremental", src=src, dest=dest)
        assert first["ok"] and first["files"] == 2 and first["new_objects"] == 2
        write(os.path.join(src, "a.txt"), "alpha v2")
        second = call("backup_incremental", src=src, dest=dest)
        assert second["unchanged"] == 1 and second["new_objects"] == 1
        snaps = call("backup_incremental", action="list", src=src, dest=dest)["snapshots"]
        assert len(snaps) == 2
        assert call("backup_incremental", action="verify", src=src, dest=dest, full=True)["ok"]
        restored = call("backup_incremental", action="restore", src=src, dest=dest, snapshot=first["snapshot"])
        assert restored["ok"] and open(os.path.join(restored["target"], "a.txt")).read() == "alpha"

    def test_corruption_is_found(self, home):
        src = os.path.join(home, "data")
        dest = os.path.join(home, "bk")
        write(os.path.join(src, "a.txt"), "alpha")
        call("backup_incremental", src=src, dest=dest)
        objs = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(dest, "objects")) for f in fs]
        import zlib
        with open(objs[0], "wb") as handle:
            handle.write(zlib.compress(b"tampered"))
        r = call("backup_incremental", action="verify", src=src, dest=dest, full=True)
        assert not r["ok"] and r["problems"][0]["problem"] == "checksum mismatch"

    def test_restoring_over_files_needs_confirmation_and_snapshots(self, home):
        src = os.path.join(home, "data")
        dest = os.path.join(home, "bk")
        write(os.path.join(src, "a.txt"), "alpha")
        call("backup_incremental", src=src, dest=dest)
        write(os.path.join(src, "a.txt"), "broken")
        r = call("backup_incremental", action="restore", src=src, dest=dest, target=src)
        assert r["errors"][0]["code"] == "E_CONFIRM"
        r = call("backup_incremental", action="restore", src=src, dest=dest, target=src, confirmed=True)
        assert r["ok"] and open(os.path.join(src, "a.txt")).read() == "alpha"
        assert call("timeline")["tasks"]


class TestDeviceAndMedia:

    def test_termux_api_missing_is_explained(self, monkeypatch):
        monkeypatch.setattr(power.base, "which", lambda n: False)
        assert call("clipboard_pipe")["errors"][0]["code"] == "E_NO_API"
        assert call("share_to", text="x")["errors"][0]["code"] == "E_NO_API"
        assert call("share_to", url="ftp://x")["errors"][0]["code"] == "E_ARGS"

    def test_clipboard_with_a_fake_api(self, home, monkeypatch):
        binr = os.path.join(home, "bin")
        store = os.path.join(home, "clip")
        write(os.path.join(binr, "termux-clipboard-get"), f"#!/bin/sh\ncat {store}\n")
        write(os.path.join(binr, "termux-clipboard-set"), f"#!/bin/sh\ncat > {store}\n")
        for f in os.listdir(binr):
            os.chmod(os.path.join(binr, f), 0o755)
        monkeypatch.setenv("PATH", binr + os.pathsep + os.environ["PATH"])
        assert call("clipboard_pipe", direction="set", text="hello clip")["ok"]
        assert call("clipboard_pipe")["text"] == "hello clip"
        out = os.path.join(home, "c.txt")
        assert call("clipboard_pipe", direction="to_file", file=out)["ok"] and open(out).read() == "hello clip"

    @pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
    def test_media_presets(self, home):
        src = os.path.join(home, "in.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=96x64:rate=5",
                        "-pix_fmt", "yuv420p", src], check=True, timeout=60)
        gif = call("media_convert", input=src, output=os.path.join(home, "o.gif"), width=64, fps=5)
        assert gif["ok"] and gif["preset"] == "gif" and gif["bytes"] > 0
        thumb = call("media_convert", input=src, output=os.path.join(home, "t.jpg"), start="0")
        assert thumb["ok"] and thumb["preset"] == "thumbnail"
        again = call("media_convert", input=src, output=os.path.join(home, "o.gif"))
        assert again["errors"][0]["code"] == "E_EXISTS"
        status = call("media_convert", action="status", output=os.path.join(home, "o.gif"))
        assert status["percent"] == 100.0 and status["status"] == "done"
        assert call("media_convert", input=src, output="x.mp4", start="1; rm")["errors"][0]["code"] == "E_ARGS"

    def test_ssh_hosts_and_run(self, home):
        assert call("ssh_hosts", action="add", name="pi", host="10.0.0.9", user="pi", port=2222)["ok"]
        assert call("ssh_hosts")["hosts"][0]["port"] == 2222
        assert call("ssh_hosts", action="add", name="x", host="bad host;")["errors"][0]["code"] == "E_ARGS"
        assert call("ssh_run", host="x;y", cmd="ls")["errors"][0]["code"] in ("E_ARGS", "E_NOT_FOUND_CMD")
        assert call("ssh_run", host="pi", cmd="rm -rf /")["errors"][0]["code"] in ("E_BLOCKED", "E_NOT_FOUND_CMD")
        assert call("ssh_hosts", action="remove", name="pi")["removed"]
