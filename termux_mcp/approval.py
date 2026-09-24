import json
import subprocess
import time

TTL_SECONDS = 180
MAX_HELD = 16
ASK_TIMEOUT = 120

APPROVAL_MARKER = "TERMUX_APPROVAL:"

GRANTED = "granted"
DECLINED = "declined"
UNAVAILABLE = "unavailable"

NO_API = ("termux-api is not installed, so the device cannot be asked. "
          "Run: pkg install termux-api")

_NO_SENSOR = ("ERROR_NO_HARDWARE", "ERROR_NO_ENROLLED_FINGERPRINTS",
              "ERROR_UNSUPPORTED_OS_VERSION")

_held = {}


def key(action=None) -> str:
    return " ".join(str(action or "").split())


def _run(argv, timeout=ASK_TIMEOUT):
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return "", NO_API
    except subprocess.TimeoutExpired:
        return "", "nobody answered in time"
    except OSError as error:
        return "", str(error)
    return done.stdout or "", ""


def _json(text):
    try:
        body = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _fingerprint(reason: str):
    text, error = _run(["termux-fingerprint", "-t", "Approve", "-d", reason,
                        "-c", "Deny"])
    if error:
        return None, error

    body = _json(text)
    if body is None:
        return None, "the fingerprint prompt gave no answer"

    if body.get("auth_result") == "AUTH_RESULT_SUCCESS":
        return True, None

    errors = body.get("errors") or []
    if any(name in errors for name in _NO_SENSOR):
        return None, "no fingerprint is set up on this device"
    return False, None


def _dialog(reason: str):
    text, error = _run(["termux-dialog", "confirm", "-t", "Approve?",
                        "-i", reason])
    if error:
        return None, error

    body = _json(text)
    if body is None:
        return None, "the dialog gave no answer"
    if body.get("error"):
        return None, str(body["error"])
    return body.get("code") == -1, None


def _ask(reason: str, method: str):
    if method == "dialog":
        return _dialog(reason)
    approved, error = _fingerprint(reason)
    if approved is None and method == "auto":
        return _dialog(reason)
    return approved, error


def _prune() -> None:
    now = time.time()
    for name in [n for n, until in _held.items() if until <= now]:
        _held.pop(name, None)


def hold(action: str) -> bool:
    name = key(action)
    if not name:
        return False
    _prune()
    while len(_held) >= MAX_HELD:
        oldest = min(_held.items(), key=lambda item: item[1])[0]
        _held.pop(oldest, None)
    _held[name] = time.time() + TTL_SECONDS
    return True


def held(action: str) -> bool:
    _prune()
    return key(action) in _held


def spend(action: str) -> bool:
    _prune()
    return _held.pop(key(action), None) is not None


WIDGETS = ("text", "number", "radio", "sheet", "spinner", "checkbox", "date",
           "time", "speech")

CHOICE_WIDGETS = ("radio", "sheet", "spinner", "checkbox")

HINT_WIDGETS = ("text", "number", "speech")


def _choices(values) -> tuple:
    items = [str(v).strip() for v in values if str(v).strip()]
    if not items:
        return None, "This widget needs 'values' — the choices to offer."
    if any("\\" in item for item in items):
        return None, ("Choices cannot contain a backslash — the dialog would "
                      "read it as an escaped comma.")
    return ",".join(item.replace(",", "\\,") for item in items), None


_CLI_WIDGET = {"number": ("text", ["-n"])}


def _dialog_argv(widget: str, params: dict) -> tuple:
    cli, flags = _CLI_WIDGET.get(widget, (widget, []))
    argv = ["termux-dialog", cli, *flags]

    title = str(params.get("title") or "").strip()
    if title:
        argv += ["-t", title[:80]]

    hint = str(params.get("hint") or "").strip()
    if hint and widget in HINT_WIDGETS:
        argv += ["-i", hint[:120]]

    if widget in CHOICE_WIDGETS:
        values = params.get("values")
        if isinstance(values, str):
            values = values.split(",")
        if not isinstance(values, (list, tuple)):
            values = [values] if values else []
        joined, error = _choices(values)
        if error:
            return None, error
        argv += ["-v", joined]

    if widget == "text" and params.get("multiline"):
        argv.append("-m")

    if widget in ("date", "time"):
        fmt = str(params.get("format") or "").strip()
        if fmt and widget == "date":
            argv += ["-d", fmt[:40]]

    return argv, None


def _answer(body: dict) -> dict:
    chosen = [str(v.get("text", "")) for v in (body.get("values") or [])
              if isinstance(v, dict)]
    if chosen:
        return {"text": "The user chose: " + ", ".join(chosen),
                "is_error": False}
    answer = str(body.get("text") or "").strip()
    if not answer:
        return {"text": "The user pressed OK but gave no answer.",
                "is_error": True}
    return {"text": f"The user answered: {answer}", "is_error": False}


def run_ask_tool(params: dict) -> dict:
    p = params or {}
    widget = str(p.get("widget") or p.get("type") or "text").strip().lower()
    if widget not in WIDGETS:
        return {"text": f"Unknown widget: {widget}. Use one of: "
                        + ", ".join(WIDGETS), "is_error": True}

    argv, error = _dialog_argv(widget, p)
    if error:
        return {"text": error, "is_error": True}

    text, error = _run(argv)
    if error:
        return {"text": f"The device could not ask: {error}.", "is_error": True}

    body = _json(text)
    if body is None:
        return {"text": "The dialog gave no answer.", "is_error": True}
    if body.get("error"):
        return {"text": str(body["error"]), "is_error": True}
    if body.get("code") != -1:
        return {"text": "The user cancelled. Do not guess an answer.",
                "is_error": True}
    return _answer(body)


def outcome(text: str, status: str, is_error: bool) -> dict:
    payload = json.dumps({"status": status})
    return {"text": f"{text}\n\n{APPROVAL_MARKER}{payload}",
            "is_error": is_error}


def run_approval_tool(name: str, params: dict) -> dict:
    if name == "ask":
        return run_ask_tool(params)
    if name != "approve":
        return {"text": f"Unknown approval tool: {name}", "is_error": True}

    p = params or {}
    action = str(p.get("action") or p.get("cmd") or "").strip()
    if not action:
        return {"text": "Missing 'action' — the exact command or action the "
                        "user is approving.", "is_error": True}

    reason = str(p.get("reason") or "").strip() or f"Run: {action}"
    if len(reason) > 200:
        reason = reason[:197] + "..."

    method = str(p.get("method") or "auto").strip().lower()
    if method not in ("auto", "fingerprint", "dialog"):
        method = "auto"

    approved, error = _ask(reason, method)
    if approved is None:
        return outcome(
            f"The device could not ask: {error}. Put the question to the "
            "user in chat instead, and do not run the action yet.",
            UNAVAILABLE, True)
    if not approved:
        return outcome(f"The user did not approve. Do not run: {action}",
                       DECLINED, True)

    hold(action)
    return outcome(
        f"Approved on the device. This exact action runs within "
        f"{TTL_SECONDS // 60} minutes: {action}", GRANTED, False)


APPROVAL_TOOLS = frozenset({"approve", "ask"})
