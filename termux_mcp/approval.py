import json
import subprocess
import time

TTL_SECONDS = 180
MAX_HELD = 16
ASK_TIMEOUT = 120

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


def run_approval_tool(name: str, params: dict) -> dict:
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
        return {"text": f"The device could not ask: {error}. Put the question "
                        "to the user in chat instead, and do not run the "
                        "action yet.", "is_error": True}
    if not approved:
        return {"text": f"The user did not approve. Do not run: {action}",
                "is_error": True}

    hold(action)
    return {"text": f"Approved on the device. This exact action runs within "
                    f"{TTL_SECONDS // 60} minutes: {action}",
            "is_error": False}


APPROVAL_TOOLS = frozenset({"approve"})
