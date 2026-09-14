import json
import os
import re
import shutil
import subprocess

from .config import HOME
from .safety import safety_root, snapshot_before_write
from .utils import is_sensitive_path

THEMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes")

COLORS_PATH = os.path.join(HOME, ".termux", "colors.properties")

THEME_JSON_MARKER = "TERMUX_THEME:"
CONFIRM_MARKER = "TERMUX_CONFIRM:"

_KEYS = ("background", "foreground", "cursor") + tuple(
    f"color{i}" for i in range(22)
)

_DEFAULT_BG = "#000000"
_DEFAULT_FG = "#ffffff"

_cache = None


def _parse(text: str) -> dict:
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        sep = re.search(r"[=:]", line)
        if not sep:
            continue
        key = line[:sep.start()].strip().lstrip("*").lstrip(".").strip().lower()
        value = line[sep.end():].strip()
        if key in _KEYS and value:
            values[key] = value
    return values


def load_themes() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    themes = {}
    for shade in ("dark", "light"):
        directory = os.path.join(THEMES_DIR, shade)
        if not os.path.isdir(directory):
            continue
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".properties"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    values = _parse(handle.read())
            except OSError:
                continue
            if not any(key == "background" or key.startswith("color")
                       for key in values):
                continue
            theme_id = filename[: -len(".properties")]
            themes[(theme_id, shade)] = {
                "id": theme_id,
                "name": theme_id.replace("_", " ").replace("-", " "),
                "shade": shade,
                "is_dark": shade == "dark",
                "colors": values,
                "background": values.get("background") or _DEFAULT_BG,
                "foreground": values.get("foreground") or _DEFAULT_FG,
                "cursor": (values.get("cursor")
                           or values.get("foreground") or _DEFAULT_FG),
            }

    _cache = themes
    return themes


def find_theme(name: str, shade: str = ""):
    themes = load_themes()
    theme_id = str(name or "").strip().lower().replace(" ", "_")
    shade = str(shade or "").strip().lower()

    if theme_id.startswith(("light_", "dark_")):
        prefix, _, rest = theme_id.partition("_")
        shade, theme_id = (shade or prefix), rest

    if shade in ("dark", "light"):
        return themes.get((theme_id, shade))
    return themes.get((theme_id, "dark")) or themes.get((theme_id, "light"))


def _theme_json(theme: dict) -> str:
    return THEME_JSON_MARKER + json.dumps(theme, separators=(",", ":"))


def _no_such_theme(wanted: str) -> str:
    """The refusal for a name that is not in the library.

    Naming the near misses turns a dead end into a second attempt that works —
    without them the model tends to try the same name again.
    """
    themes = load_themes()
    probe = str(wanted or "").strip().lower().replace(" ", "_")
    close = sorted({i for i, _ in themes if probe and probe in i})[:8]
    hint = (f" Closest: {', '.join(close)}." if close
            else " Try theme_list.")
    return f"No such theme: {probe}.{hint}"


def _needs_confirm(action: str, theme_id: str) -> str:
    payload = {
        "status": "confirmation_required",
        "action": action,
        "theme": theme_id,
        "path": COLORS_PATH,
    }
    return ("This rewrites how every shell on this device looks.\n\n"
            + CONFIRM_MARKER
            + json.dumps(payload, separators=(",", ":")))


def _reload():
    try:
        done = subprocess.run(["termux-reload-settings"],
                              capture_output=True, timeout=10)
        return done.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _colors_body(theme: dict) -> str:
    colors = theme["colors"]
    lines = [f"# {theme['name']} theme for Termux"]
    for key in ("background", "foreground", "cursor"):
        if colors.get(key):
            lines.append(f"{key}={colors[key]}")
    for index in range(22):
        value = colors.get(f"color{index}")
        if value:
            lines.append(f"color{index}={value}")
    return "\n".join(lines) + "\n"


def _colors_rel() -> str:
    return os.path.relpath(COLORS_PATH, HOME)


def _latest_snapshot():
    root = safety_root("snapshots")
    if not os.path.isdir(root):
        return None
    rel = _colors_rel()
    for stamp in sorted(os.listdir(root), reverse=True):
        candidate = os.path.join(root, stamp, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def _figlet(text: str, font: str):
    argv = ["figlet"]
    if font:
        argv += ["-f", font]
    argv.append(text)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return None, "figlet is not installed. Run: pkg install figlet"
    except subprocess.TimeoutExpired:
        return None, "figlet took too long."
    if done.returncode != 0:
        detail = (done.stderr or "").strip()
        return None, detail or f"figlet exited {done.returncode}"
    art = done.stdout.rstrip("\n")
    if not art.strip():
        return None, f"figlet produced nothing for that font: {font or 'default'}"
    return art, None


def _figlet_fonts():
    try:
        done = subprocess.run(["showfigfonts"],
                              capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    names = re.findall(r"^(\S+)\s+.*:$", done.stdout, re.MULTILINE)
    return sorted(set(names)) if names else None


def run_style_tool(name: str, params: dict) -> dict:
    p = params or {}

    if name == "theme_list":
        themes = load_themes()
        if not themes:
            return {"text": "No themes are bundled.", "is_error": True}
        dark = sorted({i for (i, s) in themes if s == "dark"})
        light = sorted({i for (i, s) in themes if s == "light"})
        both = sorted(set(dark) & set(light))
        lines = [
            f"{len(themes)} themes: {len(dark)} dark, {len(light)} light, "
            f"{len(set(dark) | set(light))} distinct names.",
            "",
            f"dark ({len(dark)}): " + ", ".join(dark),
            "",
            f"light ({len(light)}): " + ", ".join(light),
        ]
        if both:
            lines += [
                "",
                f"{len(both)} of these exist in both. Naming one without a "
                "shade gives the dark variant; pass shade: \"light\" for the "
                f"other. They are: {', '.join(both)}",
            ]
        return {"text": "\n".join(lines), "is_error": False}

    if name == "theme_preview":
        wanted = str(p.get("theme") or p.get("name") or "")
        theme = find_theme(wanted, str(p.get("shade") or ""))
        if theme is None:
            return {"text": _no_such_theme(wanted), "is_error": True}
        # No confirmation and no write: this is the same palette the apply path
        # sends, minus the change. The app draws it, which is the point — a
        # name and a couple of hex values in prose tell the user nothing about
        # what they are choosing between.
        return {
            "text": (f"{theme['name']} ({theme['shade']}) — "
                     f"{len(theme['colors'])} colours, background "
                     f"{theme['background']}, foreground "
                     f"{theme['foreground']}. Nothing has been changed yet; "
                     "theme_apply puts it in place."
                     + "\n\n" + _theme_json(theme)),
            "is_error": False,
        }

    if name == "theme_apply":
        wanted = str(p.get("theme") or p.get("name") or "")
        theme = find_theme(wanted, str(p.get("shade") or ""))
        if theme is None:
            return {"text": _no_such_theme(wanted), "is_error": True}

        if is_sensitive_path(COLORS_PATH) and not p.get("confirmed"):
            return {"text": _needs_confirm("theme_apply", theme["id"]),
                    "is_error": False}

        try:
            os.makedirs(os.path.dirname(COLORS_PATH), exist_ok=True)
            if os.path.exists(COLORS_PATH):
                snapshot_before_write(COLORS_PATH)
            with open(COLORS_PATH, "w", encoding="utf-8") as handle:
                handle.write(_colors_body(theme))
        except OSError as e:
            return {"text": f"Could not write {COLORS_PATH}: {e}",
                    "is_error": True}

        reloaded = _reload()
        text = f"{theme['name']} ({theme['shade']}) applied."
        if not reloaded:
            text += (" Run termux-reload-settings to apply it if the colours "
                     "do not change.")
        text += "\n\n" + _theme_json(theme)
        return {"text": text, "is_error": False}

    if name == "theme_revert":
        to_previous = str(p.get("to") or "previous").strip().lower() != "default"

        if not p.get("confirmed"):
            return {"text": _needs_confirm("theme_revert", ""),
                    "is_error": False}

        if not to_previous:
            if os.path.exists(COLORS_PATH):
                try:
                    snapshot_before_write(COLORS_PATH)
                    os.remove(COLORS_PATH)
                except OSError as e:
                    return {"text": f"Could not reset: {e}", "is_error": True}
            reloaded = _reload()
            return {
                "text": "Terminal colours reset to the Termux defaults."
                        + ("" if reloaded else " Run termux-reload-settings to "
                           "apply it if the colours do not change."),
                "is_error": False,
            }

        snapshot = _latest_snapshot()
        if snapshot is None:
            return {
                "text": ("Nothing was saved before the current colours, so "
                         "there is no earlier version to go back to. Use "
                         "theme_revert with to: \"default\" to return to "
                         "Termux's own colours instead."),
                "is_error": False,
            }
        try:
            os.makedirs(os.path.dirname(COLORS_PATH), exist_ok=True)
            if os.path.exists(COLORS_PATH):
                snapshot_before_write(COLORS_PATH)
            shutil.copy2(snapshot, COLORS_PATH)
        except OSError as e:
            return {"text": f"Could not restore: {e}", "is_error": True}
        reloaded = _reload()
        taken = os.path.basename(os.path.dirname(snapshot))
        return {
            "text": f"Restored the colours saved at {taken}."
                    + ("" if reloaded else " Run termux-reload-settings to "
                       "apply it if the colours do not change."),
            "is_error": False,
        }

    if name == "banner_render":
        text = str(p.get("text") or "").strip()
        if not text:
            return {"text": "Missing 'text'.", "is_error": True}
        if len(text) > 60:
            return {"text": "Keep banner text under 60 characters.",
                    "is_error": True}

        art, error = _figlet(text, str(p.get("font") or "").strip())
        if error:
            fonts = _figlet_fonts()
            if fonts:
                error += ("\n\n" + f"{len(fonts)} fonts are installed: "
                          + ", ".join(fonts))
            return {"text": error, "is_error": True}
        return {"text": art, "is_error": False}

    return {"text": f"Unknown style tool: {name}", "is_error": True}


STYLE_TOOLS = frozenset({
    "theme_list", "theme_preview", "theme_apply", "theme_revert",
    "banner_render",
})
