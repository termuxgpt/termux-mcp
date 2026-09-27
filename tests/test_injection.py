import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import handler as handler_mod
from termux_mcp import mcp_bridge as bridge
from termux_mcp import shell as shell_mod
from termux_mcp.handlers import ai_power, features, terminal

PAYLOAD = ";touch /tmp/OWNED;"


def _executor_modules():
    mods = [shell_mod, handler_mod]
    for m in (features, terminal, ai_power):
        if hasattr(m, "execute_streaming"):
            mods.append(m)
    return mods


def escapes_quoting(command: str, payload: str) -> bool:
    start = 0
    while True:
        idx = command.find(payload, start)
        if idx < 0:
            return False
        if command[:idx].count("'") % 2 == 0:
            return True
        start = idx + 1


class InjectionTests(unittest.TestCase):
    PROBES = [
        ("notify", {"title": "t", "content": "c", "id": PAYLOAD}),
        ("image_process", {"action": "resize", "input": "/a.png",
                           "output": "/b.png", "width": PAYLOAD,
                           "height": PAYLOAD}),
        ("process_list", {"limit": PAYLOAD}),
        ("process_kill", {"pid": "1", "signal": PAYLOAD}),
        ("cron_add", {"schedule": "* * * * *", "command": PAYLOAD,
                      "label": PAYLOAD}),
        ("cron_remove", {"label": PAYLOAD}),
        ("smart_install", {"packages": PAYLOAD}),
        ("weather", {"city": PAYLOAD}),
        ("qrcode", {"text": PAYLOAD}),
        ("search", {"path": ".", "pattern": PAYLOAD}),
        ("ls", {"path": PAYLOAD}),
        ("mkdir", {"path": PAYLOAD}),
        ("download", {"url": PAYLOAD, "title": PAYLOAD, "description": PAYLOAD}),
        ("tts_speak", {"text": PAYLOAD, "rate": "1.0", "pitch": "1.0"}),
        ("sms_send", {"number": PAYLOAD, "text": PAYLOAD}),
        ("toast", {"text": PAYLOAD}),
        ("clipboard_set", {"text": PAYLOAD}),
        ("open_url", {"url": PAYLOAD}),
        ("share", {"text": PAYLOAD}),
        ("screenshot", {"output": PAYLOAD}),
        ("location", {"provider": PAYLOAD}),
    ]

    def _run_probe(self, tool, params, captured):
        route = bridge.route_callable(tool)
        if route is None:
            self.skipTest(f"no route for {tool}")
        before = len(captured)
        try:
            route(bridge.VirtualHandler(), params)
        except ValueError:
            return True
        except Exception:
            pass
        return len(captured) == before

    def test_no_payload_escapes_quoting(self):
        captured = []

        def grab(_handler, cmd):
            captured.append(cmd)

        patches = [mock.patch.object(m, "execute_streaming", grab)
                   for m in _executor_modules()]
        for p in patches:
            p.start()
        try:
            for tool, params in self.PROBES:
                self._run_probe(tool, params, captured)
        finally:
            for p in patches:
                p.stop()

        self.assertGreater(len(captured), 10,
                           "expected the probes to build commands")

        leaked = [(c) for c in captured if escapes_quoting(c, PAYLOAD)]
        self.assertEqual(
            leaked, [],
            "payload escaped quoting and would execute:\n" +
            "\n".join(leaked),
        )

    def test_numeric_validators_reject_injection(self):
        from termux_mcp.utils import require_int, require_number

        for bad in (PAYLOAD, "5 && rm -rf /", "$(id)", "`id`", "", "abc",
                    "5 -o /x", "1\nrm -rf /", None, True, "nan", "inf"):
            with self.assertRaises(ValueError, msg=f"require_number({bad!r})"):
                require_number(bad)
            with self.assertRaises(ValueError, msg=f"require_int({bad!r})"):
                require_int(bad)

    def test_numeric_validators_accept_valid_values(self):
        from termux_mcp.utils import require_int, require_number

        self.assertEqual(require_number("5"), "5")
        self.assertEqual(require_number(5), "5")
        self.assertEqual(require_number("1.5"), "1.5")
        self.assertEqual(require_int("42"), "42")
        with self.assertRaises(ValueError):
            require_number(500, maximum=100)


class HandlerValidationTests(unittest.TestCase):
    def test_no_undefined_json_response(self):
        for mod in (terminal, ai_power, features):
            path = mod.__file__
            assert path is not None
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("_json_response", fh.read(), mod.__name__)

    def test_json_response_is_imported_everywhere_it_is_used(self):
        for mod in (terminal, ai_power, features):
            self.assertTrue(hasattr(mod, "json_response"), mod.__name__)


class RiskGateCoverageTests(unittest.TestCase):
    @staticmethod
    def _handler():
        class _W:
            def __init__(self):
                self.buf = io.BytesIO()

            def write(self, b):
                self.buf.write(b)

        class Fake:
            def __init__(self):
                self.status = None
                self._w = _W()

            def send_response(self, status, *a):
                self.status = status

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

            @property
            def wfile(self):
                return self._w

        return Fake()

    def test_dangerous_commands_blocked_centrally(self):
        executed = []
        with mock.patch.object(shell_mod, "_run_process",
                               lambda h, c, stdin_data=None: executed.append(c)):
            for cmd in ("rm -rf /", "chmod -R 777 /", "mkfs.ext4 /dev/block/x"):
                h = self._handler()
                shell_mod.execute_streaming(h, cmd)
                self.assertEqual(h.status, 403, f"{cmd} was not blocked")
        self.assertEqual(executed, [], "a blocked command still executed")

    def test_safe_command_still_runs(self):
        executed = []
        with mock.patch.object(shell_mod, "_run_process",
                               lambda h, c, stdin_data=None: executed.append(c)):
            h = self._handler()
            shell_mod.execute_streaming(h, "echo hi")
            self.assertEqual(h.status, 200)
        self.assertEqual(executed, ["echo hi"])

    def test_previously_dead_patterns_now_fire(self):
        from termux_mcp.security import get_risk_assessment

        for cmd in ("chmod -R 777 /", "chmod -R 000 /x", "chown -R root /x"):
            self.assertTrue(get_risk_assessment(cmd)["blocked"], cmd)

    def test_origin_check_rejects_rebinding(self):
        from termux_mcp.mcp_transport_http import _origin_allowed

        self.assertTrue(_origin_allowed("127.0.0.1", ""))
        self.assertFalse(_origin_allowed("127.0.0.1", "https://evil.com"))
        self.assertFalse(_origin_allowed("evil.com", "https://evil.com"))


class SensitivePathTests(unittest.TestCase):
    def setUp(self):
        from termux_mcp.utils import is_sensitive_path
        self.sp = is_sensitive_path
        self.home = os.path.expanduser("~")

    def test_persistence_paths_are_sensitive(self):
        for rel in (".ssh/authorized_keys", ".bashrc", ".profile",
                    ".termux/boot/start.sh", ".termux/termux.properties",
                    ".zshrc"):
            self.assertTrue(self.sp(os.path.join(self.home, rel)), rel)

    def test_ordinary_paths_are_not(self):
        for rel in ("notes.txt", "projects/app/main.py", "Downloads/x.zip"):
            self.assertFalse(self.sp(os.path.join(self.home, rel)), rel)

    def test_traversal_out_of_a_sensitive_dir_is_not_sensitive(self):
        self.assertFalse(self.sp(os.path.join(self.home, ".ssh/../notes.txt")))

    def test_empty_and_unresolvable_fail_closed(self):
        self.assertFalse(self.sp(""))
        self.assertFalse(self.sp(None))  # type: ignore[arg-type]

    def test_prefix_is_covered_selectively_not_wholesale(self):
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        if os.path.realpath(prefix) != prefix.replace("\\", "/"):
            self.skipTest("$PREFIX does not resolve here (not Termux)")

        for rel in ("bin/x", "etc/ssh/sshd_config", "libexec/y"):
            self.assertTrue(self.sp(f"{prefix}/{rel}"), rel)
        for rel in ("tmp/scratch.txt", "var/log/x.log", "share/doc/readme"):
            self.assertFalse(self.sp(f"{prefix}/{rel}"), rel)


class ValidationErrorResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_mod.MCPHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, body):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_non_numeric_parameter_returns_400(self):
        status, body = self._post("/process-list", {"limit": PAYLOAD})
        self.assertEqual(status, 400, f"expected 400, got {status}: {body}")
        self.assertIn("Expected a number", body)

    def test_the_payload_is_not_echoed_into_a_command(self):
        status, _ = self._post("/process-kill", {"pid": PAYLOAD})
        self.assertEqual(status, 400)


class TempDirTests(unittest.TestCase):
    def test_tmp_dir_is_actually_writable(self):
        from termux_mcp.utils import tmp_dir

        d = tmp_dir()
        self.assertTrue(os.path.isdir(d), d)
        probe = os.path.join(d, "_termuxgpt_probe")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_no_module_hardcodes_a_bare_tmp(self):
        import re as _re

        bare = _re.compile(r"(?<![\w/])/tmp/")
        for mod in (handler_mod, features, ai_power):
            path = mod.__file__
            assert path is not None
            with open(path, encoding="utf-8") as fh:
                hits = bare.findall(fh.read())
            self.assertEqual(
                hits, [], f"{mod.__name__} still hardcodes a bare /tmp path"
            )


class RiskPatternPrecisionTests(unittest.TestCase):
    DANGEROUS = [
        "rm -rf /",
        "echo x && rm -rf /",
        "echo x && rm -rf / && echo y",
        "ls; rm -rf /",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf ~/*",
        "rm -rf /*",
        "rm -rf --no-preserve-root /",
        "chmod -R 777 /",
        "dd if=/dev/zero of=/dev/block/x",
        "mkfs.ext4 /dev/block/x",
    ]

    NOT_DANGEROUS = [
        "echo hi && rm -rf /data/data/com.termux/files/usr/tmp/migrate_* 2>/dev/null",
        "rm -rf ~/junk",
        "rm -rf ./build",
        "ls -la",
        "echo ok",
    ]

    def test_dangerous_targets_are_blocked(self):
        from termux_mcp.security import get_risk_assessment

        for cmd in self.DANGEROUS:
            res = get_risk_assessment(cmd)
            self.assertTrue(res["blocked"], f"not blocked: {cmd}")

    def test_ordinary_commands_are_not_blocked(self):
        from termux_mcp.security import get_risk_assessment

        for cmd in self.NOT_DANGEROUS:
            res = get_risk_assessment(cmd)
            self.assertFalse(res["blocked"], f"wrongly blocked: {cmd}")


if __name__ == "__main__":
    unittest.main()
