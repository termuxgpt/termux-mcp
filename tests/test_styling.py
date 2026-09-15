import json
import os
import sys
from unittest import mock

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
            "font", "banner_render",
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

    def test_preview_a_list_of_themes(self):
        result = run_style_tool(
            "theme_preview", {"themes": ["dracula", "nord", "gruvbox"]})
        assert not result["is_error"]
        payloads = result["text"].split(styling.THEME_JSON_MARKER)[1:]
        ids = [json.loads(p.strip().split("\n", 1)[0])["id"] for p in payloads]
        assert ids == ["dracula", "nord", "gruvbox"]
        assert result["text"].count("Nothing has been changed yet") == 3

    def test_preview_list_is_the_same_as_one_at_a_time(self):
        one = run_style_tool("theme_preview", {"theme": "dracula"})["text"]
        many = run_style_tool("theme_preview", {"themes": ["dracula"]})["text"]
        assert one == many

    def test_preview_list_names_the_ones_it_could_not_find(self):
        result = run_style_tool(
            "theme_preview", {"themes": ["dracula", "definitely_not_a_theme"]})
        assert not result["is_error"]
        assert "Not found: definitely_not_a_theme." in result["text"]
        assert result["text"].count(styling.THEME_JSON_MARKER) == 1

    def test_preview_list_with_nothing_left_is_an_error(self):
        result = run_style_tool("theme_preview", {"themes": ["nope", "also_nope"]})
        assert result["is_error"]
        assert "No such theme" in result["text"]

    def test_preview_list_is_capped(self):
        names = sorted({i for i, s in load_themes() if s == "dark"})
        result = run_style_tool("theme_preview", {"themes": names})
        assert result["text"].count(styling.THEME_JSON_MARKER) == \
            styling.MAX_PREVIEWS
        assert "left out" in result["text"]

    def test_preview_list_accepts_a_comma_separated_string(self):
        result = run_style_tool("theme_preview", {"themes": "nord, gruvbox"})
        assert not result["is_error"]
        assert result["text"].count(styling.THEME_JSON_MARKER) == 2

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


class TestFonts:
    """The font tool draws; it must never install or change anything.

    The renderer is mocked rather than run, so these hold on a machine with
    neither figlet nor toilet — which is where they are written.
    """

    def test_parses_showfigfonts(self):
        sample = ("standard  Sample text:\n\nbig  Sample:\n\n"
                  "slant   Sample:\n")
        assert styling._parse_figlet_fonts(sample) == ["big", "slant", "standard"]

    def test_parsing_nothing_gives_nothing(self):
        assert styling._parse_figlet_fonts("") is None
        assert styling._parse_figlet_fonts("no fonts here") is None

    def test_list_says_how_to_install_when_there_are_none(self):
        with mock.patch.object(styling, "_figlet_fonts", return_value=None), \
             mock.patch.object(styling, "_toilet_fonts", return_value=[]):
            result = run_style_tool("font", {"action": "list"})
        assert result["is_error"]
        assert "pkg install figlet toilet" in result["text"]

    def test_list_separates_the_two_libraries_and_filters(self):
        with mock.patch.object(styling, "_figlet_fonts",
                               return_value=["doom", "slant", "small"]), \
             mock.patch.object(styling, "_toilet_fonts", return_value=["term"]):
            all_fonts = run_style_tool("font", {"action": "list"})["text"]
            filtered = run_style_tool("font", {"action": "list",
                                               "query": "sla"})["text"]
        assert "figlet (3):" in all_fonts and "toilet (1):" in all_fonts
        assert "slant" in filtered
        assert "doom" not in filtered
        assert "1 of 3" in filtered

    def test_a_query_that_matches_nothing_is_an_error(self):
        with mock.patch.object(styling, "_figlet_fonts",
                               return_value=["doom"]), \
             mock.patch.object(styling, "_toilet_fonts", return_value=[]):
            assert run_style_tool("font", {"action": "list",
                                           "query": "zzz"})["is_error"]

    def test_preview_labels_every_font(self):
        with mock.patch.object(styling, "_render_font",
                               return_value=("ART", None)):
            result = run_style_tool("font", {"action": "preview", "text": "Hi",
                                             "fonts": "big,slant"})
        assert not result["is_error"]
        assert "── big" in result["text"]
        assert "── slant" in result["text"]
        assert result["text"].count("ART") == 2

    def test_preview_keeps_going_when_one_font_fails(self):
        def fake(text, font, filter_=""):
            return (None, f"no such font: {font}") if font == "nope" \
                else ("ART", None)

        with mock.patch.object(styling, "_render_font", side_effect=fake):
            result = run_style_tool("font", {"action": "preview", "text": "Hi",
                                             "fonts": "big,nope"})
        assert "── big" in result["text"]
        assert "skipped" in result["text"] and "nope" in result["text"]

    def test_preview_with_no_font_named_draws_the_defaults(self):
        seen = []

        def fake(text, font, filter_=""):
            seen.append((text, font))
            return ("ART", None)

        with mock.patch.object(styling, "_render_font", side_effect=fake):
            run_style_tool("font", {"action": "preview"})
        assert [f for _, f in seen] == list(styling._DEFAULT_SAMPLE_FONTS)
        assert seen[0][0] == "Termux"

    def test_preview_refuses_a_sample_too_wide_for_the_fonts(self):
        result = run_style_tool("font", {"action": "preview", "text": "x" * 41})
        assert result["is_error"]

    def test_preview_renders_nothing_when_every_font_fails(self):
        with mock.patch.object(styling, "_render_font",
                               return_value=(None, "figlet is not installed")), \
             mock.patch.object(styling, "_figlet_fonts", return_value=None):
            result = run_style_tool("font", {"action": "preview", "text": "Hi"})
        assert result["is_error"]
        assert "not installed" in result["text"]

    def test_unknown_action_is_refused(self):
        result = run_style_tool("font", {"action": "install"})
        assert result["is_error"]
        assert "list or preview" in result["text"]
