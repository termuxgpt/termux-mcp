import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from termux_mcp import styling
from termux_mcp.styling import (_colors_body, _parse, find_theme, load_themes,
                                run_style_tool)


def _colours_state():
    """The colours file as it is, so a test can prove nothing touched it.

    None when there is no file — which is also the answer for "did this write
    one", since the state has to match before and after.
    """
    if not os.path.exists(styling.COLORS_PATH):
        return None
    with open(styling.COLORS_PATH, "rb") as handle:
        return (os.stat(styling.COLORS_PATH).st_mtime_ns, handle.read())


class TestParser:

    def test_properties_dialect(self):
        v = _parse("background=#282a36\nforeground=#f8f8f2\n")
        assert v["background"] == "#282a36"
        assert v["foreground"] == "#f8f8f2"

    def test_xresources_dialect(self):
        v = _parse("background: #0a0f14\nforeground: #98d1ce\n")
        assert v["background"] == "#0a0f14"

    def test_xresources_tab_padded(self):
        v = _parse("color0\t\t\t: #263238\n")
        assert v["color0"] == "#263238"

    def test_star_prefixes_stripped(self):
        v = _parse("*.background: #000000\n*foreground: #ffffff\n")
        assert v["background"] == "#000000"
        assert v["foreground"] == "#ffffff"

    def test_comments_and_blanks_skipped(self):
        v = _parse("# a comment\n! another\n\nbackground=#111111\n")
        assert list(v) == ["background"]

    def test_colour_keys_are_lowercased(self):
        assert _parse("Background=#111111")["background"] == "#111111"

    def test_unknown_keys_ignored(self):
        v = _parse("background=#111111\nsomethingElse=yes\n")
        assert "somethingelse" not in v

    def test_empty_value_ignored(self):
        assert "background" not in _parse("background=\n")


class TestLibrary:

    def test_every_bundled_file_loads(self):
        themes = load_themes()
        on_disk = set()
        for shade in ("dark", "light"):
            directory = os.path.join(styling.THEMES_DIR, shade)
            if os.path.isdir(directory):
                on_disk |= {
                    f[: -len(".properties")]
                    for f in os.listdir(directory)
                    if f.endswith(".properties")
                }
        loaded = {theme_id for (theme_id, _) in themes}
        missing = sorted(on_disk - loaded)
        assert not missing, f"themes dropped by the loader: {missing}"

    def test_both_shades_survive_shared_names(self):
        themes = load_themes()
        dark = {i for (i, s) in themes if s == "dark"}
        light = {i for (i, s) in themes if s == "light"}
        shared = dark & light
        assert shared, "expected some names in both shades"
        for name in shared:
            assert (name, "dark") in themes
            assert (name, "light") in themes

    def test_themes_without_background_still_load(self):
        for name in ("gnometerm", "pastel_neutral"):
            theme = find_theme(name)
            assert theme is not None, f"{name} was dropped"
            assert theme["colors"].get("color0")
            assert theme["background"].startswith("#")
            assert theme["foreground"].startswith("#")


class TestFindTheme:

    def test_bare_name_prefers_dark(self):
        assert find_theme("gruvbox")["shade"] == "dark"

    def test_explicit_shade(self):
        assert find_theme("gruvbox", "light")["shade"] == "light"

    def test_shade_prefix_in_the_name(self):
        assert find_theme("light gruvbox")["shade"] == "light"

    def test_underscores_and_case(self):
        assert find_theme("Solarized New")["id"] == "solarized_new"

    def test_unknown_returns_none(self):
        assert find_theme("definitely_not_a_theme") is None


class TestTools:

    def test_only_the_style_tools(self):
        assert styling.STYLE_TOOLS == {
            "theme_list", "theme_preview", "theme_apply", "theme_revert",
            "banner_render",
        }

    def test_list_reports_the_library(self):
        text = run_style_tool("theme_list", {})["text"]
        assert "dark" in text and "light" in text
        assert "exist in both" in text

    def test_preview_returns_the_palette(self):
        result = run_style_tool("theme_preview", {"theme": "dracula"})
        assert not result["is_error"]
        assert styling.THEME_JSON_MARKER in result["text"]
        payload = result["text"].split(styling.THEME_JSON_MARKER, 1)[1].strip()
        theme = json.loads(payload)
        assert theme["id"] == "dracula"
        assert theme["shade"] == "dark"
        assert theme["background"] == "#282a36"
        assert theme["colors"]["color0"]

    def test_preview_needs_no_confirmation(self):
        # It changes nothing, so it must not spend a confirmation — and it must
        # not be mistaken for an apply by whoever reads the result.
        text = run_style_tool("theme_preview", {"theme": "dracula"})["text"]
        assert styling.CONFIRM_MARKER not in text
        assert "Nothing has been changed" in text

    def test_preview_writes_nothing(self):
        before = _colours_state()
        run_style_tool("theme_preview", {"theme": "dracula"})
        run_style_tool("theme_preview", {"theme": "solarized", "shade": "light"})
        assert _colours_state() == before

    def test_preview_unknown_theme_is_an_error(self):
        result = run_style_tool("theme_preview", {"theme": "nope"})
        assert result["is_error"]
        assert "No such theme" in result["text"]

    def test_apply_requires_confirmation(self):
        result = run_style_tool("theme_apply", {"theme": "dracula"})
        assert not result["is_error"]
        assert styling.CONFIRM_MARKER in result["text"]
        assert "confirmation_required" in result["text"]

    def test_revert_requires_confirmation(self):
        assert styling.CONFIRM_MARKER in run_style_tool("theme_revert", {})["text"]

    def test_revert_accepts_both_targets(self):
        for target in ("previous", "default"):
            result = run_style_tool("theme_revert", {"to": target})
            assert styling.CONFIRM_MARKER in result["text"]

    def test_unknown_theme_is_an_error(self):
        result = run_style_tool("theme_apply",
                                {"theme": "nope", "confirmed": True})
        assert result["is_error"]

    def test_banner_needs_text(self):
        assert run_style_tool("banner_render", {})["is_error"]

    def test_banner_rejects_absurd_text(self):
        assert run_style_tool("banner_render", {"text": "x" * 200})["is_error"]

    def test_body_writes_only_what_the_theme_has(self):
        theme = find_theme("gnometerm")
        body = _colors_body(theme)
        assert "foreground=" not in body
        assert "background=" not in body
        assert "color0=" in body
