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


if __name__ == "__main__":
    unittest.main()
