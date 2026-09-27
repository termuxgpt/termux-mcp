import math
import re
import threading
from collections import Counter

BANK = {
    "battery": ("battery", {}, True, [
        "how much battery do i have", "battery percentage", "is my phone charging", "phone charge level",
        "how long will my battery last", "battery health", "power left", "am i plugged in"]),
    "wifi_info": ("wifi_info", {}, True, [
        "which wifi am i on", "network name", "am i connected to the internet", "wifi signal",
        "connection info", "what network is this"]),
    "public_ip": ("public_ip", {}, True, [
        "what is my ip", "show my external address", "my internet address", "public address"]),
    "system_info": ("system_info", {}, True, [
        "phone specs", "how much ram", "cpu usage", "is my phone overheating", "device information",
        "memory used", "how hot is the phone", "processor info"]),
    "storage_clean": ("storage_clean", {"dry_run": True}, True, [
        "free up space", "no room left", "my disk is full", "phone storage is full", "clear junk",
        "delete caches", "not enough space", "running out of storage", "clean my termux", "reclaim space"]),
    "disk_usage": ("run", {"cmd": "df -h $HOME"}, True, [
        "how much space is left", "disk usage", "storage used", "how full is my storage", "free disk space"]),
    "process_list": ("process_list", {}, True, [
        "what is running", "show processes", "what is using my cpu", "list running programs",
        "which apps are running", "top processes"]),
    "health_watch": ("health_watch", {"action": "run"}, True, [
        "is everything ok", "check my phone health", "diagnose termux", "something is wrong with termux",
        "run a health check", "why is termux slow"]),
    "cost_meter": ("cost_meter", {}, True, [
        "how many tokens did i save", "token savings", "show the cost meter", "how much did i save"]),
    "changes_list": ("changes_list", {}, True, [
        "what did you change", "show recent edits", "which files were modified", "list changes"]),
    "timeline": ("timeline", {}, True, [
        "show task history", "what have you done today", "timeline of changes", "history of tasks"]),
    "cron_list": ("cron_list", {}, True, [
        "what is scheduled", "show my cron jobs", "list scheduled tasks", "my timers"]),
    "watch_list": ("watch_list", {}, True, [
        "show my automations", "list my triggers", "what rules do i have", "my event rules"]),
    "project_profile": ("project_profile", {}, True, [
        "how do i run this project", "what kind of project is this", "how to start this app",
        "what language is this repo"]),
    "git_sync": ("git_sync", {}, False, [
        "update my repo", "pull the latest code", "sync with github", "get latest changes from remote"]),
    "pkg_update": ("run", {"cmd": "pkg update -y && pkg upgrade -y"}, False, [
        "update everything", "upgrade all packages", "update termux", "get the latest packages"]),
    "graph_query": ("graph_query", {"q": "what is installed"}, True, [
        "what is installed", "list installed tools", "which languages do i have", "show my packages"]),
    "policy_get": ("policy_get", {}, True, [
        "show the policy", "what am i allowed to do", "what are the limits", "safety settings"]),
    "metrics": ("metrics", {}, True, [
        "show server metrics", "tool latency", "how fast are the tools", "error rates"]),
    "screen_ocr": ("screen_ocr", {}, True, [
        "read the text on my screen", "what does my screen say", "copy text from screen", "ocr the screen"]),
    "clipboard_get": ("clipboard_pipe", {"direction": "get"}, True, [
        "what is in my clipboard", "paste what i copied", "show the clipboard", "read clipboard"]),
    "tunnel_list": ("tunnel", {"action": "list"}, True, [
        "show open tunnels", "which ports are public", "list my tunnels"]),
    "ssh_hosts": ("ssh_hosts", {"action": "list"}, True, [
        "list my servers", "saved ssh hosts", "which machines can i ssh into"]),
}

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("i me my the a an is are am do does did to of on in for it this that what which how "
                  "can you please show me get".split())

_lock = threading.Lock()
_index = {"docs": None, "idf": None}


def _features(text: str) -> Counter:
    t = " " + " ".join(_TOKEN.findall(str(text or "").lower())) + " "
    feats = Counter()
    for i in range(len(t) - 2):
        tri = t[i:i + 3]
        if tri.strip():
            feats["c:" + tri] += 1
    for w in _TOKEN.findall(t):
        if w not in _STOP:
            feats["w:" + w] += 3
    return feats


def _vector(feats: Counter, idf: dict) -> dict:
    vec = {k: (1 + math.log(v)) * idf.get(k, 0.0) for k, v in feats.items()}
    norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
    return {k: x / norm for k, x in vec.items()}


def _documents():
    docs = []
    for intent, (tool, params, ro, phrases) in BANK.items():
        for phrase in phrases + [intent.replace("_", " ")]:
            docs.append({"intent": intent, "tool": tool, "params": dict(params), "read_only": ro, "text": phrase})
    try:
        from ..playbook import load_playbooks
        for pid, pb in load_playbooks()[0].items():
            for phrase in (pb.get("phrases") or []) + [pb.get("title") or ""]:
                if phrase:
                    docs.append({"intent": f"playbook:{pid}", "tool": "do", "params": {}, "read_only": False,
                                 "text": re.sub(r"\{[a-z_0-9]+\}", " ", phrase)})
    except Exception:
        pass
    return docs


def _build():
    docs = _documents()
    df = Counter()
    feats = []
    for d in docs:
        f = _features(d["text"])
        feats.append(f)
        df.update(set(f))
    n = len(docs) or 1
    idf = {k: math.log((1 + n) / (1 + v)) + 1.0 for k, v in df.items()}
    for d, f in zip(docs, feats):
        d["vec"] = _vector(f, idf)
    return docs, idf


def invalidate() -> None:
    with _lock:
        _index["docs"] = None
        _index["idf"] = None


def _ensure():
    with _lock:
        if _index["docs"] is None:
            _index["docs"], _index["idf"] = _build()
        return _index["docs"], _index["idf"]


def match(text: str, k: int = 3) -> list:
    docs, idf = _ensure()
    q = _vector(_features(text), idf)
    if not q:
        return []
    best = {}
    for d in docs:
        vec = d["vec"]
        score = sum(w * vec.get(key, 0.0) for key, w in q.items())
        if score > best.get(d["intent"], (0.0, None))[0]:
            best[d["intent"]] = (score, d)
    ranked = sorted(best.values(), key=lambda x: -x[0])[:k]
    return [{"intent": d["intent"], "tool": d["tool"], "params": dict(d["params"]), "read_only": d["read_only"],
             "score": round(s, 3), "phrase": d["text"]} for s, d in ranked if s > 0]


SURE = 0.62
MAYBE = 0.45


def confidence(score: float) -> float:
    if score >= SURE:
        return round(min(0.86 + (score - SURE) * 0.3, 0.93), 2)
    if score >= MAYBE:
        return round(0.6 + (score - MAYBE) / (SURE - MAYBE) * 0.2, 2)
    return round(score, 2)
