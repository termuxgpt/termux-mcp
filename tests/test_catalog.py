import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import mcp_core as core
from termux_mcp.tools_schema import build_catalog


def _names(catalog):
    return [entry["name"] for entry in catalog]


class TestCatalog:

    def test_every_offered_tool_is_in_the_catalog(self):
        offered = [d["name"] for d in core.NATIVE_TOOL_DEFS]
        catalog = _names(build_catalog(core.NATIVE_TOOL_DEFS))
        assert [name for name in offered if name not in catalog] == []

    def test_the_styling_family_is_no_longer_missing(self):
        catalog = _names(build_catalog(core.NATIVE_TOOL_DEFS))
        for name in ("theme_list", "theme_preview", "theme_apply",
                     "theme_revert", "font", "banner_render"):
            assert name in catalog

    def test_no_name_appears_twice(self):
        catalog = _names(build_catalog(core.NATIVE_TOOL_DEFS))
        assert len(catalog) == len(set(catalog))

    def test_openai_entries_win_the_overlap(self):
        catalog = {e["name"]: e for e in build_catalog(core.NATIVE_TOOL_DEFS)}
        assert catalog["run"]["category"] == "shell"
        assert catalog["terminal_open"]["category"] == "terminal"

    def test_extras_carry_a_category_and_params(self):
        catalog = {e["name"]: e for e in build_catalog(core.NATIVE_TOOL_DEFS)}
        assert catalog["theme_apply"]["category"] == "appearance"
        assert catalog["approve"]["category"] == "safety"
        assert catalog["ask"]["category"] == "device"
        assert "widget" in catalog["ask"]["params"]

    def test_a_tool_with_no_schema_still_gets_a_row(self):
        catalog = {e["name"]: e for e in build_catalog(
            [{"name": "bare", "description": "no schema"}])}
        assert catalog["bare"]["params"] == ""
        assert catalog["bare"]["category"] == "other"

    def test_without_extras_it_still_works(self):
        assert _names(build_catalog())
