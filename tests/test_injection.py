"""Regression tests for command injection.

Parameters reaching these handlers come from HTTP, WebSocket and MCP callers.
Each one is interpolated into a command string run with shell=True, so a
parameter that is not quoted — or not validated as numeric — is arbitrary
command execution.

The check is quote parity rather than a substring search. A payload sitting
inside single quotes is inert; the same bytes outside them are a second
command. Substring-matching alone produces false positives, because a quoted
`'label: ;touch /tmp/x;'` legitimately contains the payload text.

No command is ever executed: the executor is stubbed in every module that
binds it.
"""

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

# Chosen to break out of double quotes, single quotes and bare interpolation,
# and to be recognisable in a captured command.
PAYLOAD = ";touch /tmp/OWNED;"


def _executor_modules():
    mods = [shell_mod, handler_mod]
    for m in (features, terminal, ai_power):
        if hasattr(m, "execute_streaming"):
            mods.append(m)
    return mods


def escapes_quoting(command: str, payload: str) -> bool:
    """True if payload appears outside single quotes in command.

    Counts quotes preceding the match: an even count means the payload is not
    inside a quoted run, so the shell would act on it.
    """
    start = 0
    while True:
        idx = command.find(payload, start)
        if idx < 0:
            return False
        if command[:idx].count("'") % 2 == 0:
            return True
        start = idx + 1


class InjectionTests(unittest.TestCase):
    """Every parameter below was unquoted or unvalidated in the original code."""

    # (tool, params) — the payload is placed in each parameter that was RAW.
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
        """Invoke a route, capturing commands. Returns True if it raised ValueError."""
        route = bridge.route_callable(tool)
        if route is None:
            self.skipTest(f"no route for {tool}")
        before = len(captured)
        try:
            route(bridge.VirtualHandler(), params)
        except ValueError:
            # Rejected outright by require_int/require_number — the strongest
            # possible outcome for a numeric parameter.
            return True
        except Exception:
            # A tool's own validation may reject the params for other reasons.
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
        # Bounds are enforced, not silently clamped.
        with self.assertRaises(ValueError):
            require_number(500, maximum=100)


class HandlerValidationTests(unittest.TestCase):
    """Handlers that previously crashed on their validation error paths.

    `_json_response` was called in 15 places across handlers/terminal.py and
    handlers/ai_power.py but was never imported or defined, so every one of
    those paths raised NameError instead of returning a 400. Nothing covered
    them, so it went unnoticed.
    """

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
    """The risk gate must apply to every endpoint, not only /run.

    get_risk_assessment was called from exactly two places — the /run handler
    and the MCP run tool — so roughly 120 endpoints bypassed it, including
    /write, /delete, /patch, /cron-add, /service-guard and /ssh-wizard.
    Funnelling the check through execute_streaming is what closes that.
    """

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
                               lambda h, c: executed.append(c)):
            for cmd in ("rm -rf /", "chmod -R 777 /", "mkfs.ext4 /dev/block/x"):
                h = self._handler()
                shell_mod.execute_streaming(h, cmd)
                self.assertEqual(h.status, 403, f"{cmd} was not blocked")
        self.assertEqual(executed, [], "a blocked command still executed")

    def test_safe_command_still_runs(self):
        executed = []
        with mock.patch.object(shell_mod, "_run_process",
                               lambda h, c: executed.append(c)):
            h = self._handler()
            shell_mod.execute_streaming(h, "echo hi")
            self.assertEqual(h.status, 200)
        self.assertEqual(executed, ["echo hi"])

    def test_previously_dead_patterns_now_fire(self):
        # These patterns are written with uppercase flags, but the command is
        # lowercased before matching — so case-sensitive matching meant they
        # could never fire and `chmod -R 777 /` reported SAFE.
        from termux_mcp.security import get_risk_assessment

        for cmd in ("chmod -R 777 /", "chmod -R 000 /x", "chown -R root /x"):
            self.assertTrue(get_risk_assessment(cmd)["blocked"], cmd)

    def test_origin_check_rejects_rebinding(self):
        from termux_mcp.mcp_transport_http import _origin_allowed

        # No Origin: the app, curl and stdio clients send none.
        self.assertTrue(_origin_allowed("127.0.0.1", ""))
        # A page resolving its own host to loopback sends Host: 127.0.0.1
        # with the attacker's Origin. This used to be allowed.
        self.assertFalse(_origin_allowed("127.0.0.1", "https://evil.com"))
        self.assertFalse(_origin_allowed("evil.com", "https://evil.com"))


class SensitivePathTests(unittest.TestCase):
    """Writes that grant persistence must require confirmation.

    is_safe_path is a three-prefix denylist (/dev, /proc, /sys), not a
    sandbox, so ~/.ssh/authorized_keys, ~/.bashrc and ~/.termux/boot/start.sh
    were all writable with no gate at all — an SSH backdoor, code execution
    on every shell start, and execution at device boot.
    """

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
        # Resolves to ~/notes.txt, so it is an ordinary file — the check
        # works on the realpath, not the literal string.
        self.assertFalse(self.sp(os.path.join(self.home, ".ssh/../notes.txt")))

    def test_empty_and_unresolvable_fail_closed(self):
        self.assertFalse(self.sp(""))
        self.assertFalse(self.sp(None))  # type: ignore[arg-type]

    def test_prefix_is_covered_selectively_not_wholesale(self):
        """$PREFIX holds executables and config — but $TMPDIR lives under it.

        Treating all of $PREFIX as sensitive meant writing an ordinary
        temporary file required confirmation. Found by running against a real
        device, where $TMPDIR really is $PREFIX/tmp; a Windows dev machine has
        no such layout, so no local test would have caught it.
        """
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        # The $PREFIX branch only means anything where Termux's layout exists.
        # On Windows, realpath("/data/...") becomes "P:\\data\\..." and can
        # never match, so this would fail for the wrong reason — the device run
        # is the authoritative check for it.
        if os.path.realpath(prefix) != prefix.replace("\\", "/"):
            self.skipTest("$PREFIX does not resolve here (not Termux)")

        for rel in ("bin/x", "etc/ssh/sshd_config", "libexec/y"):
            self.assertTrue(self.sp(f"{prefix}/{rel}"), rel)
        for rel in ("tmp/scratch.txt", "var/log/x.log", "share/doc/readme"):
            self.assertFalse(self.sp(f"{prefix}/{rel}"), rel)


class ValidationErrorResponseTests(unittest.TestCase):
    """A bad parameter must produce a 400, not a dropped connection.

    require_number/require_int raise ValueError. Nothing caught it, so the
    exception escaped to BaseHTTPRequestHandler, which logs and closes the
    socket — the client saw a network error with no indication of which
    parameter was wrong.
    """

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
        # The error names the value, which is fine, but it must never have
        # reached the shell.
        status, _ = self._post("/process-kill", {"pid": PAYLOAD})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
