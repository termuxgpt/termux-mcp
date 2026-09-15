import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import mcp_bridge as bridge, mcp_core as core
from termux_mcp.styling import STYLE_TOOLS
from termux_mcp.terminal import TERMINAL_TOOLS
from termux_mcp.tools_schema import OPENAI_TOOLS

NATIVE = {d["name"] for d in core.NATIVE_TOOL_DEFS}


def routable(name: str) -> bool:
    return (name in NATIVE or name in STYLE_TOOLS or name in TERMINAL_TOOLS
            or bridge.route_callable(name) is not None)


class TestRegistryDrift:

    def test_the_drop_list_is_only_the_documented_aliases(self):
        assert set(bridge.WS_ALIAS_DROPS) == {"camera", "wifi", "sms", "tts",
                                              "ocr"}

    def test_every_advertised_tool_is_reachable_or_deliberately_dropped(self):
        stranded = [e["function"]["name"] for e in OPENAI_TOOLS
                    if not routable(e["function"]["name"])
                    and e["function"]["name"] not in bridge.WS_ALIAS_DROPS]
        assert stranded == [], (
            "advertised in tools_schema.py with nothing behind them: "
            f"{stranded}")

    def test_every_tool_a_client_is_offered_can_actually_run(self):
        offered = [d["name"] for d in bridge.build_mcp_tool_list(core.NATIVE_TOOL_DEFS)]
        broken = [name for name in offered if not routable(name)]
        assert broken == [], f"offered but not routable: {broken}"

    def test_every_directly_implemented_tool_is_advertised(self):
        stray = sorted(set(bridge.NATIVE_TOOL_NAMES) - NATIVE)
        assert stray == [], f"implemented in mcp_core but never offered: {stray}"

    def test_every_advertised_native_tool_is_implemented_somewhere(self):
        handled = set(bridge.NATIVE_TOOL_NAMES) | STYLE_TOOLS | TERMINAL_TOOLS
        unimplemented = sorted(NATIVE - handled)
        assert unimplemented == [], \
            f"offered to clients with nothing behind them: {unimplemented}"

    def test_a_schema_and_a_route_do_not_disagree_about_arguments(self):
        for entry in OPENAI_TOOLS:
            function = entry["function"]
            if not routable(function["name"]):
                continue
            params = function.get("parameters", {})
            assert params.get("type") == "object", function["name"]
            for required in params.get("required", []):
                assert required in params.get("properties", {}), \
                    f"{function['name']} requires {required}, which it does not declare"
