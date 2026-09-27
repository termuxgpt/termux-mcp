import base64
import json
import os
import shlex

from .. import safety
from . import base

DOTFILES = [".bashrc", ".zshrc", ".profile", ".bash_profile", ".gitconfig", ".vimrc", ".nanorc",
            ".tmux.conf", ".termux/termux.properties", ".termux/colors.properties", ".ssh/config"]
MAX_DOTFILE = 64 * 1024


def snapshot(name: str = "") -> dict:
    home = base.home()
    manual = base.sh("apt-mark showmanual 2>/dev/null", timeout=30, check_risk=False).out.split()
    versions = {}
    q = base.sh("dpkg-query -W -f='${Package}\\t${Version}\\n' 2>/dev/null", timeout=30, check_risk=False)
    for line in q.out.splitlines():
        if "\t" in line:
            p, v = line.split("\t", 1)
            versions[p] = v
    pip = [l for l in base.sh("pip list --not-required --format=freeze 2>/dev/null", timeout=60,
                              check_risk=False).out.splitlines() if "==" in l]
    npm = base.sh("npm ls -g --depth=0 --parseable 2>/dev/null", timeout=60, check_risk=False).out.splitlines()
    npm = [os.path.basename(p) for p in npm[1:] if p.strip()]
    dots = {}
    for rel in DOTFILES:
        path = os.path.join(home, rel)
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= MAX_DOTFILE:
                with open(path, "rb") as handle:
                    dots[rel] = base64.b64encode(handle.read()).decode()
        except OSError:
            pass
    cron = base.sh("crontab -l 2>/dev/null", timeout=10, check_risk=False).out
    from . import intent_tools
    services = [{"name": n, "cmd": c, "port": p} for n, c, p, _, _ in intent_tools.service_table()]
    twin = {"twin_version": 1, "created": base.now_iso(), "arch": os.uname().machine if hasattr(os, "uname") else "",
            "packages": {p: versions.get(p, "") for p in (manual or list(versions))},
            "pip": pip, "npm": npm, "dotfiles": dots, "cron": cron, "services": services}
    folder = base.ensure_dir(base.root("twins"))
    fname = (name or "twin-" + base.now_iso().replace(":", "").replace("-", "")) + ".json"
    path = os.path.join(folder, os.path.basename(fname))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(twin, handle)
    return {"ok": True, "file": path, "packages": len(twin["packages"]), "pip": len(pip), "npm": len(npm),
            "dotfiles": sorted(dots), "cron_lines": len([l for l in cron.splitlines() if l.strip()]),
            "services": len(services),
            "summary": f"twin saved: {len(twin['packages'])} packages, {len(pip)} pip, {len(dots)} dotfiles → {path}"}


def restore(file: str, dry_run: bool = True, confirmed: bool = False, parts=None) -> dict:
    try:
        with open(os.path.expanduser(file), encoding="utf-8") as handle:
            twin = json.load(handle)
    except (OSError, ValueError) as error:
        return {"ok": False, "errors": [{"code": "E_NO_FILE", "msg": str(error)}]}
    parts = parts or ["packages", "pip", "npm", "dotfiles", "cron"]
    home = base.home()
    have = set(base.sh("dpkg-query -W -f='${Package}\\n' 2>/dev/null", timeout=30, check_risk=False).out.split())
    missing = sorted(p for p in twin.get("packages", {}) if p not in have)
    pip_have = {l.split("==")[0].lower() for l in base.sh("pip list --format=freeze 2>/dev/null", timeout=60,
                                                          check_risk=False).out.splitlines() if "==" in l}
    pip_missing = [l for l in twin.get("pip", []) if l.split("==")[0].lower() not in pip_have]
    plan = {"packages": missing, "pip": pip_missing, "npm": twin.get("npm", []),
            "dotfiles": sorted(twin.get("dotfiles", {})), "cron": bool(twin.get("cron", "").strip())}
    plan = {k: v for k, v in plan.items() if k in parts}
    if dry_run or not confirmed:
        return {"ok": True, "dry_run": True, "plan": plan,
                "summary": f"would install {len(plan.get('packages', []))} packages, {len(plan.get('pip', []))} pip, "
                           f"write {len(plan.get('dotfiles', []))} dotfiles; resend dry_run:false confirmed:true",
                **({} if dry_run else {"needs_confirmation": True})}
    report = {}
    if plan.get("packages"):
        res = base.sh("pkg install -y " + " ".join(shlex.quote(p) for p in plan["packages"]), timeout=1800,
                      confirmed=True)
        report["packages"] = "ok" if res.ok else "some failed"
    if plan.get("pip"):
        res = base.sh("pip install --break-system-packages " + " ".join(shlex.quote(p.split("==")[0])
                                                                        for p in plan["pip"]),
                      timeout=1800, confirmed=True)
        report["pip"] = "ok" if res.ok else "some failed"
    if plan.get("npm"):
        res = base.sh("npm install -g " + " ".join(shlex.quote(p) for p in plan["npm"]), timeout=1800,
                      confirmed=True)
        report["npm"] = "ok" if res.ok else "some failed"
    if plan.get("dotfiles"):
        for rel, blob in twin.get("dotfiles", {}).items():
            dst = os.path.join(home, rel)
            safety.snapshot_before_write(dst, tool="restore_env")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as handle:
                handle.write(base64.b64decode(blob))
        report["dotfiles"] = len(twin.get("dotfiles", {}))
    if plan.get("cron"):
        res = base.sh("crontab -", stdin=twin["cron"], timeout=10, check_risk=False)
        report["cron"] = "ok" if res.ok else "failed"
    from . import cache
    cache.invalidate("pkg", "graph", "context")
    return {"ok": all(v not in ("some failed", "failed") for v in report.values()), "restored": report,
            "summary": "restored: " + ", ".join(f"{k} {v}" for k, v in report.items())}
