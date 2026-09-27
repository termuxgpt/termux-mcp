import os
import re
import sqlite3
import threading
import time

from . import base, cache

_lock = threading.Lock()

MANIFESTS = {
    "package.json": "node", "requirements.txt": "python", "pyproject.toml": "python",
    "setup.py": "python", "Cargo.toml": "rust", "go.mod": "go", "pubspec.yaml": "dart",
    "Makefile": "make", "CMakeLists.txt": "cmake", "Gemfile": "ruby", "composer.json": "php",
    "pom.xml": "java", "build.gradle": "java",
}


def db_path() -> str:
    return os.path.join(base.ensure_dir(base.root("state")), "graph.db")


def _conn():
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS packages (name TEXT PRIMARY KEY, version TEXT, manager TEXT);
        CREATE TABLE IF NOT EXISTS commands (name TEXT PRIMARY KEY, path TEXT);
        CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, lang TEXT, manifest TEXT, git INTEGER);
        CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, cmd TEXT, port INTEGER, pid INTEGER, updated TEXT);
        CREATE TABLE IF NOT EXISTS cron (line TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


def refresh(parts=("packages", "commands", "projects", "services", "cron")) -> dict:
    counts = {}
    with _lock:
        conn = _conn()
        try:
            if "packages" in parts:
                res = base.sh("dpkg-query -W -f='${Package}\\t${Version}\\n' 2>/dev/null",
                              timeout=30, check_risk=False)
                rows = [l.split("\t", 1) for l in res.out.splitlines() if "\t" in l]
                pip = base.sh("pip list --format=freeze 2>/dev/null", timeout=30,
                              check_risk=False)
                prow = [l.split("==", 1) for l in pip.out.splitlines() if "==" in l]
                conn.execute("DELETE FROM packages")
                conn.executemany("INSERT OR REPLACE INTO packages VALUES (?,?,'pkg')", rows)
                conn.executemany("INSERT OR REPLACE INTO packages VALUES (?,?,'pip')",
                                 [(f"pip:{n.lower()}", v) for n, v in prow])
                counts["packages"] = len(rows) + len(prow)
            if "commands" in parts:
                seen = {}
                for folder in os.environ.get("PATH", "").split(os.pathsep):
                    try:
                        for name in os.listdir(folder):
                            full = os.path.join(folder, name)
                            if name not in seen and os.access(full, os.X_OK):
                                seen[name] = full
                    except OSError:
                        continue
                conn.execute("DELETE FROM commands")
                conn.executemany("INSERT OR REPLACE INTO commands VALUES (?,?)", seen.items())
                counts["commands"] = len(seen)
            if "projects" in parts:
                found = scan_projects(base.home())
                conn.execute("DELETE FROM projects")
                conn.executemany("INSERT OR REPLACE INTO projects VALUES (?,?,?,?)", found)
                counts["projects"] = len(found)
            if "services" in parts:
                from . import intent_tools
                svc = intent_tools.service_table()
                conn.execute("DELETE FROM services")
                conn.executemany("INSERT OR REPLACE INTO services VALUES (?,?,?,?,?)", svc)
                counts["services"] = len(svc)
            if "cron" in parts:
                res = base.sh("crontab -l 2>/dev/null", timeout=10, check_risk=False)
                lines = [l for l in res.out.splitlines() if l.strip() and not l.startswith("#")]
                conn.execute("DELETE FROM cron")
                conn.executemany("INSERT OR REPLACE INTO cron VALUES (?)", [(l,) for l in lines])
                counts["cron"] = len(lines)
            _set_meta(conn, "refreshed", base.now_iso())
            conn.commit()
        finally:
            conn.close()
    cache.invalidate("graph")
    return {"ok": True, "summary": "graph refreshed: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
            "facts": counts}


def scan_projects(start: str, depth: int = 3) -> list:
    out = []
    start = os.path.abspath(start)
    skip = {"node_modules", ".git", "termuxGPT", "storage", ".cache", "venv", ".venv",
            "__pycache__", "build", "dist", "target", ".npm", ".cargo"}
    for dirpath, dirnames, filenames in os.walk(start):
        rel_depth = dirpath[len(start):].count(os.sep)
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")] \
            if rel_depth < depth else []
        for manifest, lang in MANIFESTS.items():
            if manifest in filenames:
                out.append((dirpath, lang, manifest,
                            1 if os.path.isdir(os.path.join(dirpath, ".git")) else 0))
                break
        if len(out) >= 200:
            break
    return out


def _ensure_fresh():
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='refreshed'").fetchone()
        finally:
            conn.close()
    if not row:
        refresh()


def on_package_change(names=()) -> None:
    try:
        refresh(parts=("packages", "commands"))
    except Exception:
        pass


def query(q: str) -> dict:
    _ensure_fresh()
    text = str(q or "").strip()
    low = text.lower()
    with _lock:
        conn = _conn()
        try:
            def pkg(name):
                name = name.lower()
                r = conn.execute("SELECT name, version, manager FROM packages WHERE lower(name)=? "
                                 "OR lower(name)=?", (name, f"pip:{name}")).fetchall()
                c = conn.execute("SELECT path FROM commands WHERE name=?", (name,)).fetchone()
                return r, c

            if re.search(r"\b(projects?|repos?)\b", low):
                rows = conn.execute("SELECT path, lang, manifest, git FROM projects ORDER BY path").fetchall()
                return {"ok": True, "summary": f"{len(rows)} projects",
                        "facts": {"projects": [{"path": p, "lang": l, "manifest": m, "git": bool(g)}
                                               for p, l, m, g in rows[:50]]}}
            if re.search(r"\b(services?|servers?|running)\b", low):
                rows = conn.execute("SELECT name, cmd, port, pid FROM services").fetchall()
                return {"ok": True, "summary": f"{len(rows)} managed services",
                        "facts": {"services": [{"name": n, "cmd": c, "port": p, "pid": d}
                                               for n, c, p, d in rows]}}
            if re.search(r"\b(cron|scheduled|jobs?)\b", low):
                rows = [r[0] for r in conn.execute("SELECT line FROM cron").fetchall()]
                return {"ok": True, "summary": f"{len(rows)} cron jobs", "facts": {"cron": rows}}
            if re.search(r"\b(how many|count).*(packages?)\b", low):
                n = conn.execute("SELECT count(*) FROM packages WHERE manager='pkg'").fetchone()[0]
                p = conn.execute("SELECT count(*) FROM packages WHERE manager='pip'").fetchone()[0]
                return {"ok": True, "summary": f"{n} pkg packages, {p} pip packages",
                        "facts": {"pkg": n, "pip": p}}
            m = re.search(r"(?:is|are|do i have|have)\s+([\w.+-]+)\s+(?:installed|there|available)", low) \
                or re.search(r"(?:version of|which version|what version)\s+(?:is\s+)?([\w.+-]+)", low) \
                or re.search(r"^([\w.+-]+)\s+(?:version|installed\??)$", low) \
                or re.search(r"^([\w.+-]+)\??$", low)
            if m:
                name = m.group(1)
                rows, cmd = pkg(name)
                if rows or cmd:
                    facts = {"installed": True, "name": name}
                    if rows:
                        facts["versions"] = {r[0]: r[1] for r in rows}
                    if cmd:
                        facts["path"] = cmd[0]
                    ver = rows[0][1] if rows else "on PATH"
                    return {"ok": True, "summary": f"{name}: installed ({ver})", "facts": facts}
                return {"ok": True, "summary": f"{name}: not installed",
                        "facts": {"installed": False, "name": name}}
            like = conn.execute("SELECT name, version FROM packages WHERE name LIKE ? LIMIT 20",
                                (f"%{low[:30]}%",)).fetchall()
            return {"ok": True, "summary": f"{len(like)} matching packages",
                    "facts": {"matches": {n: v for n, v in like}}}
        finally:
            conn.close()


def stats() -> dict:
    _ensure_fresh()
    with _lock:
        conn = _conn()
        try:
            out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                   for t in ("packages", "commands", "projects", "services", "cron")}
            row = conn.execute("SELECT value FROM meta WHERE key='refreshed'").fetchone()
            out["refreshed"] = row[0] if row else ""
            return out
        finally:
            conn.close()
