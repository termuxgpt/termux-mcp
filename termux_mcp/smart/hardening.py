import os
import re
import stat

from . import base

SECRET_PATTERNS = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", "termuxGPT", "storage", ".venv", "venv", ".npm", ".cargo"}
TEXT_EXT = {"", ".py", ".js", ".ts", ".json", ".env", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt",
            ".md", ".dart", ".go", ".rb", ".php", ".conf"}


def mask(secret: str) -> str:
    return secret[:4] + "…" + secret[-2:] if len(secret) > 8 else "…"


def scan_secrets(root: str, max_files: int = 4000, max_bytes: int = 512 * 1024) -> list:
    found = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if os.path.basename(dirpath) == ".ssh":
            dirnames[:] = []
            continue
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT and not name.startswith(".env"):
                continue
            path = os.path.join(dirpath, name)
            seen += 1
            if seen > max_files:
                return found
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    for lineno, line in enumerate(handle, 1):
                        for label, pattern in SECRET_PATTERNS:
                            m = pattern.search(line)
                            if m:
                                found.append({"file": path, "line": lineno, "kind": label, "preview": mask(m.group(0))})
                                break
            except OSError:
                continue
            if len(found) >= 50:
                return found
    return found


def loose_keys(home: str) -> list:
    out = []
    ssh = os.path.join(home, ".ssh")
    if not os.path.isdir(ssh):
        return out
    for name in os.listdir(ssh):
        path = os.path.join(ssh, name)
        if not os.path.isfile(path) or name.endswith(".pub") or name in ("known_hosts", "config", "authorized_keys"):
            continue
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            out.append({"file": path, "mode": oct(mode), "fix": f"chmod 600 {path}"})
    return out


def parse_listeners(text: str) -> list:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or "LISTEN" not in line:
            continue
        local = next((p for p in parts if re.search(r":\d+$", p)), "")
        if not local:
            continue
        host, _, port = local.rpartition(":")
        host = host.strip("[]")
        if host in ("127.0.0.1", "::1", "localhost"):
            continue
        proc = re.search(r'users:\(\("([^"]+)"', line) or re.search(r"\d+/(\S+)", line)
        out.append({"address": host or "*", "port": int(port), "process": proc.group(1) if proc else ""})
    return out


def check() -> dict:
    from .. import config
    home = base.home()
    items = []

    def add(name, ok, detail, fix=""):
        items.append({"check": name, "ok": ok, "detail": detail, **({"fix": fix} if fix and not ok else {})})

    exposed_mcp = config.HOST not in ("127.0.0.1", "localhost", "::1")
    add("termux_mcp", not (exposed_mcp and not config.REQUIRE_AUTH),
        f"listening on {config.HOST}" + (", auth on" if config.REQUIRE_AUTH else ", auth off"),
        "export TERMUX_MCP_HOST=127.0.0.1 (or set TERMUX_MCP_AUTH_TOKEN) and restart termux-mcp")

    ls = base.sh("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null", timeout=10, check_risk=False)
    open_ports = parse_listeners(ls.out)
    add("open_ports", not open_ports,
        "nothing reachable from the network" if not open_ports
        else ", ".join(f"{p['process'] or '?'}:{p['port']}" for p in open_ports),
        "bind these servers to 127.0.0.1, or stop the ones you don't need")

    sshd_conf = os.path.join(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "etc", "ssh", "sshd_config")
    sshd_running = base.sh("pgrep -x sshd >/dev/null", timeout=5, check_risk=False).ok
    if sshd_running:
        text = ""
        try:
            with open(sshd_conf, encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
        except OSError:
            pass
        password = not re.search(r"(?mi)^\s*PasswordAuthentication\s+no\b", text)
        add("ssh", not password, "sshd running" + (", password login allowed" if password else ", keys only"),
            "set PasswordAuthentication no in sshd_config after adding your key to ~/.ssh/authorized_keys")

    keys = loose_keys(home)
    add("ssh_keys", not keys, "private keys locked down" if not keys else f"{len(keys)} key(s) readable by others",
        "; ".join(k["fix"] for k in keys))

    secrets = scan_secrets(home)
    add("secrets", not secrets, "no keys found in project files" if not secrets
        else f"{len(secrets)} possible secret(s) in files", "move them to an .env file outside the project and rotate them")

    up = base.sh("apt list --upgradable 2>/dev/null | grep -c upgradable", timeout=60, check_risk=False)
    n = int(up.out.strip() or 0) if up.out.strip().isdigit() else 0
    add("updates", n == 0, "all packages up to date" if n == 0 else f"{n} package(s) can be upgraded", "pkg upgrade -y")

    bad = [i for i in items if not i["ok"]]
    return {"ok": True, "secure": not bad, "problems": len(bad), "checks": items, "open_ports": open_ports,
            "secrets": secrets, "loose_keys": keys,
            "summary": "looks safe" if not bad else "; ".join(f"{i['check']}: {i['detail']}" for i in bad)}
