import re

HIGH = 0.85
MEDIUM = 0.6

_PORT = r"(?:port\s*)?:?(\d{2,5})"
_PKG = r"([a-z0-9][a-z0-9.+_-]*)"

RULES = [
    ("battery", r"\b(battery|charg(e|ing)|power level)\b", lambda m, t: {}, 0.92, True),
    ("wifi_info", r"\b(wi-?fi|ssid|signal strength|which network)\b", lambda m, t: {}, 0.9, True),
    ("public_ip", r"\b(public|external|my) ip\b|\bip address\b", lambda m, t: {}, 0.88, True),
    ("system_info", r"\b(cpu|ram|memory usage|uptime|temperature|system info|specs)\b", lambda m, t: {}, 0.88, True),
    ("storage_clean", r"\b(free (up )?(some )?space|clean ?up|clear cache|storage full|low storage)\b",
     lambda m, t: {"dry_run": True}, 0.86, True),
    ("disk_usage", r"\b(disk|storage|space)\b.*\b(left|free|used|usage|how much)\b|\bhow much (space|storage)\b",
     lambda m, t: {}, 0.9, True),
    ("process_list", r"\b(running (processes|apps)|what'?s running|top processes|process list|"
                     r"what('?s| is) (using|eating|hogging) (my |the )?(cpu|ram|memory|battery))\b",
     lambda m, t: {}, 0.9, True),
    ("port_free", r"\b(free|kill|stop|release)\b.*\bport\b\s*:?(\d{2,5})|\bport (\d{2,5})\b.*\b(busy|in use|free it)\b",
     lambda m, t: {"port": int(next(g for g in m.groups()[1:] if g and g.isdigit()))}, 0.9, False),
    ("pkg_ensure", r"\b(install|add|get me|set ?up)\s+" + _PKG + r"(?:\s+(?:and|,)\s+" + _PKG + r")?\s*$",
     lambda m, t: {"names": [g for g in (m.group(2), m.group(3)) if g]}, 0.88, False),
    ("graph_query", r"\b(is|do i have|have i got)\s+" + _PKG + r"\s+installed\b|\bversion of\s+" + _PKG,
     lambda m, t: {"q": t}, 0.9, True),
    ("git_sync", r"\b(git )?(sync|pull|update)\s+(my )?(repo|repository|project)\b|\bgit sync\b",
     lambda m, t: {}, 0.8, False),
    ("changes_list", r"\bwhat (did you|have you) change(d)?\b|\brecent changes\b|\bshow changes\b",
     lambda m, t: {}, 0.9, True),
    ("undo", r"^\s*undo( (that|it|the last( change)?))?\s*[.!]?$", lambda m, t: {"limit": 1}, 0.85, False),
    ("cron_list", r"\b(scheduled|cron) (jobs|tasks)\b|\bwhat'?s scheduled\b", lambda m, t: {}, 0.9, True),
    ("watch_list", r"\b(my )?(watches|triggers|automations|rules)\b", lambda m, t: {}, 0.75, True),
    ("health_watch", r"\b(health( check)?|is (my|the) (phone|termux) ok|diagnose)\b",
     lambda m, t: {"action": "run"}, 0.85, True),
    ("cost_meter", r"\b(tokens? saved|how much (did i|have i) save|cost meter)\b", lambda m, t: {}, 0.9, True),
    ("project_profile", r"\bhow (do i|to) run (this|the|my) (project|app|repo)\b", lambda m, t: {}, 0.8, True),
    ("backup", r"^\s*(back ?up|make a backup)( my)?( (home|configs?|files))?\s*$",
     lambda m, t: {"target": "configs" if "config" in t else "home"}, 0.85, False),
    ("pkg_update", r"^\s*(update|upgrade)( (my )?(packages|termux|everything|all))?\s*$",
     lambda m, t: {}, 0.85, False),
    ("explain_cmd", r"^\s*(explain|what does)\s+[`'\"]?(.+?)[`'\"]?( do| mean)?\??$",
     lambda m, t: {"cmd": _command_like(m.group(2))}, 0.88, True),
    ("device_graph", r"\b(my projects|list (my )?projects|what('?s| is) installed)\b",
     lambda m, t: {"q": t}, 0.82, True),
]

_COMPILED = [(tool, re.compile(rx, re.IGNORECASE), build, conf, ro) for tool, rx, build, conf, ro in RULES]

_NEGATIONS = re.compile(r"\b(when|whenever|every|if|schedule|remind|script|write a|create a|automate|"
                        r"benchmark|debug|configure|deploy|generate|refactor|migrate|translate|analy[sz]e|"
                        r"compile|build)\b", re.IGNORECASE)


def _command_like(text: str) -> str:
    t = str(text or "").strip().strip("`'\"")
    first = (t.split() or [""])[0]
    if re.search(r"(^|\s)-{1,2}\w|[|<>;&]|\$\(|\d{3,4}\b", t):
        return t
    from shutil import which
    if first and (which(first) or first in ("cd", "export", "alias", "source", "echo", "set")):
        return t
    raise ValueError("not a command")


def _playbook_fit(text: str, cand: dict) -> float:
    try:
        from ..playbook import words
    except Exception:
        return 1.0
    asked = words(text)
    for value in (cand.get("inputs") or {}).values():
        asked -= words(str(value))
    if not asked:
        return 1.0
    best = 0.0
    for phrase in cand.get("phrases") or []:
        best = max(best, len(words(re.sub(r"\{[a-z_0-9]+\}", " ", phrase)) & asked) / len(asked))
    return best

ALIASES = {"disk_usage": ("run", lambda p: {"cmd": "df -h $HOME"}),
           "pkg_update": ("run", lambda p: {"cmd": "pkg update -y && pkg upgrade -y"}),
           "device_graph": ("graph_query", lambda p: p)}


def route(text: str) -> dict:
    t = str(text or "").strip()
    if not t:
        return {"ok": True, "confidence": 0.0, "route": "ai", "candidates": []}
    words = len(t.split())
    penalty = 0.0
    if words > 14:
        penalty += 0.15
    if _NEGATIONS.search(t):
        penalty += 0.35
    cands = []
    for tool, rx, build, conf, ro in _COMPILED:
        m = rx.search(t)
        if not m:
            continue
        try:
            params = build(m, t)
        except (StopIteration, ValueError, TypeError, IndexError):
            continue
        score = max(conf - penalty, 0.0)
        real, mapper = ALIASES.get(tool, (tool, lambda p: p))
        cands.append({"tool": real, "params": mapper(params), "confidence": round(score, 2),
                      "read_only": ro, "intent": tool})
    try:
        from . import semantic
        regex_intents = {c["intent"] for c in cands}
        ceiling = max((c["confidence"] for c in cands), default=0.0)
        for m in semantic.match(t, 3):
            if m["intent"] in regex_intents or m["intent"].startswith("playbook:"):
                continue
            score = max(semantic.confidence(m["score"]) - penalty, 0.0)
            if ceiling >= MEDIUM:
                score = min(score, round(ceiling - 0.01, 2))
            if score < 0.3:
                continue
            cands.append({"tool": m["tool"], "params": m["params"], "confidence": round(score, 2),
                          "read_only": m["read_only"], "intent": m["intent"], "via": "semantic"})
    except Exception:
        pass
    try:
        from ..playbook import match_text
        for c in match_text(t)[:3]:
            fit = _playbook_fit(t, c)
            score = min(float(c["score"]), 0.95) * (0.6 + 0.4 * fit) - penalty
            if score > 0.3:
                cands.append({"tool": "do", "params": {"text": t}, "confidence": round(score, 2),
                              "read_only": False, "intent": f"playbook:{c['playbook']}",
                              "missing": c.get("missing") or []})
    except Exception:
        pass
    cands.sort(key=lambda c: -c["confidence"])
    uniq, seen = [], set()
    for c in cands:
        key = (c["tool"], c["intent"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    best = uniq[0]["confidence"] if uniq else 0.0
    decision = "local" if best >= HIGH else "choose" if best >= MEDIUM else "ai"
    if decision == "local" and uniq[0].get("missing"):
        decision = "choose"
    return {"ok": True, "route": decision, "confidence": best, "candidates": uniq[:3],
            "summary": {"local": f"run {uniq[0]['tool']} locally" if uniq else "",
                        "choose": "offer these options", "ai": "send to the model"}[decision]}
