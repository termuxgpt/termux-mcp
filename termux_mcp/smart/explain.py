import re
import shlex

from ..security import get_risk_assessment
from . import base

COMMANDS = {
    "ls": "list directory contents", "cd": "change directory", "pwd": "print working directory",
    "cat": "print file contents", "less": "page through a file", "head": "first lines of a file",
    "tail": "last lines of a file", "grep": "search text for a pattern", "find": "search for files",
    "cp": "copy files", "mv": "move/rename files", "rm": "delete files", "mkdir": "create directories",
    "rmdir": "remove empty directories", "touch": "create a file / update its time", "chmod": "change permissions",
    "chown": "change owner", "ln": "create links", "du": "disk usage of files", "df": "free disk space",
    "tar": "pack/unpack archives", "zip": "create zip archives", "unzip": "extract zip archives",
    "gzip": "compress files", "curl": "transfer data from/to a URL", "wget": "download files",
    "ssh": "remote shell over SSH", "scp": "copy over SSH", "rsync": "sync files efficiently",
    "git": "version control", "pkg": "Termux package manager", "apt": "Debian package manager",
    "pip": "Python package installer", "pip3": "Python package installer", "npm": "Node package manager",
    "node": "run JavaScript", "python": "run Python", "python3": "run Python", "bash": "run a bash shell/script",
    "sh": "run a POSIX shell/script", "echo": "print text", "printf": "formatted print", "sed": "stream editor",
    "awk": "pattern scanning and processing", "sort": "sort lines", "uniq": "remove duplicate lines",
    "wc": "count lines/words/bytes", "xargs": "build commands from input", "tee": "write to file and stdout",
    "kill": "send a signal to a process", "pkill": "kill processes by name", "ps": "list processes",
    "top": "live process monitor", "htop": "interactive process monitor", "nohup": "keep running after logout",
    "sleep": "wait", "watch": "re-run a command periodically", "crontab": "edit scheduled jobs",
    "tmux": "terminal multiplexer", "vim": "text editor", "nano": "simple text editor", "make": "build with a Makefile",
    "clang": "C/C++ compiler", "gcc": "C compiler", "ffmpeg": "convert audio/video", "chmod+x": "",
    "termux-setup-storage": "grant access to shared storage", "termux-battery-status": "battery info (Termux:API)",
    "termux-wifi-connectioninfo": "Wi-Fi info (Termux:API)", "termux-notification": "show a notification",
    "termux-change-repo": "pick a package mirror", "whoami": "current user", "uname": "system information",
    "env": "show/modify environment", "export": "set an environment variable", "source": "run a script in this shell",
    "which": "locate a command", "man": "manual page", "history": "command history", "date": "date and time",
    "ping": "test network reachability", "nslookup": "DNS lookup", "ss": "socket statistics", "netstat": "network connections",
    "lsof": "list open files/ports", "fuser": "who uses a file/port", "dd": "raw copy (dangerous on devices)",
    "mount": "mount filesystems", "sudo": "run as root (absent on Termux)", "su": "switch user (root needed)",
}

FLAGS = {
    ("rm", "-r"): "recursive: remove directories and their contents",
    ("rm", "-f"): "force: never ask, ignore missing files",
    ("rm", "-rf"): "recursive + force: delete everything below, no questions",
    ("ls", "-l"): "long listing (permissions, size, date)", ("ls", "-a"): "include hidden files",
    ("ls", "-h"): "human-readable sizes", ("ls", "-la"): "long listing including hidden files",
    ("cp", "-r"): "copy directories recursively", ("mkdir", "-p"): "create parents, no error if exists",
    ("grep", "-r"): "search recursively", ("grep", "-i"): "ignore case", ("grep", "-n"): "show line numbers",
    ("grep", "-v"): "invert match", ("tar", "-x"): "extract", ("tar", "-c"): "create", ("tar", "-z"): "gzip",
    ("tar", "-f"): "archive file name", ("tar", "-xzf"): "extract a .tar.gz", ("tar", "-czf"): "create a .tar.gz",
    ("curl", "-O"): "save with the remote file name", ("curl", "-L"): "follow redirects", ("curl", "-s"): "silent",
    ("pkg", "-y"): "assume yes", ("apt", "-y"): "assume yes", ("git", "--force"): "overwrite remote history",
    ("chmod", "+x"): "make executable", ("chmod", "-R"): "recursive", ("du", "-sh"): "total size, human-readable",
    ("df", "-h"): "human-readable sizes", ("kill", "-9"): "SIGKILL: force-kill, no cleanup",
    ("ps", "aux"): "all processes with details", ("tail", "-f"): "follow new lines", ("head", "-n"): "number of lines",
}

_OPS = {"|": "pipe output into the next command", "&&": "run next only if this succeeded",
        "||": "run next only if this failed", ";": "run next regardless", ">": "overwrite a file with output",
        ">>": "append output to a file", "<": "read input from a file", "2>&1": "merge errors into output",
        "&": "run in background"}

_cache = {}


def _describe(cmd: str) -> str:
    if cmd in COMMANDS and COMMANDS[cmd]:
        return COMMANDS[cmd]
    if cmd in _cache:
        return _cache[cmd]
    desc = ""
    res = base.sh(f"whatis {shlex.quote(cmd)} 2>/dev/null | head -1", timeout=3, check_risk=False)
    if res.ok and " - " in res.out:
        desc = res.out.split(" - ", 1)[1].strip()
    if not desc and base.which(cmd):
        res = base.sh(f"{shlex.quote(cmd)} --help 2>&1 | head -3", timeout=3, check_risk=False)
        line = next((l.strip() for l in res.out.splitlines() if l.strip() and not l.lower().startswith("usage")), "")
        desc = line[:100]
    _cache[cmd] = desc or "unknown command"
    return _cache[cmd]


def explain(cmd: str) -> dict:
    text = str(cmd or "").strip()
    if not text:
        return {"ok": False, "errors": [{"code": "E_ARGS", "msg": "cmd required"}]}
    tokens = re.split(r"(\|\||&&|;|\||>>|2>&1|>|<|&(?!&))", text)
    parts = []
    effects = set()
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in _OPS:
            parts.append({"op": tok, "means": _OPS[tok]})
            if tok in (">", ">>"):
                effects.add("writes a file")
            continue
        try:
            words = shlex.split(tok)
        except ValueError:
            words = tok.split()
        if not words:
            continue
        name = words[0]
        entry = {"cmd": name, "means": _describe(name)}
        flags = []
        for w in words[1:]:
            key = (name, w)
            if key in FLAGS:
                flags.append({"flag": w, "means": FLAGS[key]})
            elif w.startswith("-") and len(w) > 2 and not w.startswith("--"):
                for ch in w[1:]:
                    k = (name, f"-{ch}")
                    if k in FLAGS:
                        flags.append({"flag": f"-{ch}", "means": FLAGS[k]})
        if flags:
            entry["flags"] = flags
        args = [w for w in words[1:] if not w.startswith("-")]
        if args:
            entry["args"] = args[:6]
        if name in ("rm", "dd", "mkfs", "shred") or (name == "git" and "--force" in words):
            effects.add("destructive")
        if name in ("curl", "wget", "ssh", "scp", "rsync", "git", "pip", "npm", "pkg", "apt"):
            effects.add("uses the network")
        if name in ("pkg", "apt", "pip", "pip3", "npm") and any(w in words for w in ("install", "remove", "uninstall")):
            effects.add("changes installed software")
        parts.append(entry)
    risk = get_risk_assessment(text)
    return {"ok": True, "cmd": text, "parts": parts, "effects": sorted(effects),
            "risk": risk["risk_level"], "risk_note": risk["message"],
            "summary": " then ".join(p["means"] for p in parts if "cmd" in p)[:200]}
