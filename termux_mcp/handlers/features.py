import os
import re
import time
from typing import TYPE_CHECKING

from ..safety import snapshot_before_write
from ..shell import execute_streaming, get_current_dir
from ..utils import (shell_quote, require_int, is_safe_path, is_sensitive_path,
                     json_response, tmp_dir)

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")

# A cron schedule is five space-separated fields of digits and cron
# operators. The crontab line is written with the schedule interpolated in,
# so anything else (quotes, `;`, `$(...)`) is rejected rather than echoed
# into the crontab.
CRON_FIELD = r"[0-9*/,\-]+"
CRON_SCHEDULE_RE = re.compile(rf"{CRON_FIELD}(?: {CRON_FIELD}){{4}}")

# `gh pr list --state` / `gh pr merge --<method>` accept only these.
PR_STATES = ("open", "closed", "merged", "all")
PR_MERGE_METHODS = ("merge", "squash", "rebase")


def handle_system_info(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    cmd = (
        'cpu=$(top -bn1 2>/dev/null | grep -oP "[0-9.]+%" | head -1 | tr -d "%" || echo 0);'
        'ram_total=$(free -m 2>/dev/null | awk "/Mem:/{print \\$2}" || echo 0);'
        'ram_used=$(free -m 2>/dev/null | awk "/Mem:/{print \\$3}" || echo 0);'
        'disk_total=$(df -m /data 2>/dev/null | awk "END{print \\$2}" || echo 0);'
        'disk_used=$(df -m /data 2>/dev/null | awk "END{print \\$3}" || echo 0);'
        'temp=0; [ -r /sys/class/thermal/thermal_zone0/temp ] && temp=$(($(cat /sys/class/thermal/thermal_zone0/temp)/1000));'
        'uptime=0; [ -r /proc/uptime ] && uptime=$(awk "{print int(\\$1)}" /proc/uptime);'
        'echo "{\\"cpu_percent\\":\\"$cpu\\",\\"ram_mb_total\\":$ram_total,\\"ram_mb_used\\":$ram_used,\\"disk_mb_total\\":$disk_total,\\"disk_mb_used\\":$disk_used,\\"temp_celsius\\":$temp,\\"uptime_seconds\\":$uptime}"'
    )
    execute_streaming(handler, cmd)


def handle_process_list(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    limit = require_int(data.get("limit", 20))
    cmd = (
        f'echo "PID USER CPU% MEM% COMMAND";'
        f'ps aux --sort=-%cpu 2>/dev/null | head -n {limit} | '
        f'while read u p c m r; do printf "%-6s %-8s %-5s %-5s %s\\n" "$p" "$u" "$c" "$m" "$r"; done'
    )
    execute_streaming(handler, cmd)


def handle_process_kill(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    # require_int replaces the old `.isdigit()` gate on pid and validates the
    # signal too. minimum=1 keeps the gate's guarantee that a negative pid
    # ("-1" kills every process the user can signal) never reaches kill(1).
    try:
        pid = require_int(data.get("pid", ""), minimum=1)
        signal_num = require_int(data.get("signal", 15))
    except ValueError:
        json_response(handler, 400, {"error": "Valid PID required"})
        return

    cmd = (
        f'echo "Killing PID {pid} ...";'
        f'kill -{signal_num} {pid} 2>&1 && echo "Process {pid} terminated" '
        f'|| echo "Failed to kill process {pid}"'
    )
    execute_streaming(handler, cmd)


def handle_cron_add(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    schedule = data.get("schedule", "").strip()
    command = data.get("command", "").strip()
    label = data.get("label", "termux_task").strip()

    if not schedule or not command:
        json_response(handler, 400, {"error": "schedule and command required"})
        return

    if not CRON_SCHEDULE_RE.fullmatch(schedule):
        json_response(handler, 400, {
            "error": "Invalid schedule - expected 5 cron fields, e.g. '*/5 * * * *'"})
        return

    cmd = (
        f'echo {shell_quote("Adding cron job: " + label)};'
        f'(crontab -l 2>/dev/null; echo {shell_quote("# " + label)};'
        f'echo {shell_quote(schedule + " " + command)}) | crontab - 2>&1 && '
        f'echo {shell_quote("Cron job added: " + schedule + " " + command)} '
        f'|| echo "Failed. Install: pkg install cronie termux-services"'
    )
    execute_streaming(handler, cmd)


def handle_cron_list(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    cmd = (
        'echo "Cron Jobs:";'
        'echo "---";'
        'crontab -l 2>&1 || echo "No cron jobs. Install: pkg install cronie termux-services"'
    )
    execute_streaming(handler, cmd)


def handle_cron_remove(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    label = data.get("label", "").strip()

    if label:
        safe = shell_quote(label)
        cmd = (
            f'crontab -l 2>/dev/null | grep -v {safe} | crontab - 2>&1 && '
            f'echo {shell_quote("Removed cron jobs matching: " + label)} '
            f'|| echo "Failed to remove"'
        )
    else:
        cmd = (
            'crontab -r 2>&1 && echo "All cron jobs removed" '
            '|| echo "No cron jobs to remove"'
        )
    execute_streaming(handler, cmd)


def handle_diff(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    file1 = data.get("file", "").strip()
    file2 = data.get("file2", "").strip()

    if not file1 or not is_safe_path(file1):
        json_response(handler, 400, {"error": "Valid file path required"})
        return

    safe1 = shell_quote(file1)

    if file2:
        if not is_safe_path(file2):
            json_response(handler, 400, {"error": "Invalid file2 path"})
            return
        safe2 = shell_quote(file2)
        cmd = f'diff -u {safe1} {safe2} 2>&1 || echo "(files differ or one missing)"'
    else:
        cmd = (
            f'echo {shell_quote("File: " + file1)};'
            f'wc -l {safe1} 2>/dev/null;'
            f'echo "Last modified: $(stat -c %y {safe1} 2>/dev/null)";'
            f'echo "Size: $(stat -c %s {safe1} 2>/dev/null) bytes"'
        )
    execute_streaming(handler, cmd)


def handle_patch(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    target = data.get("file", "").strip()
    patch_content = data.get("patch", "")

    if not target or not is_safe_path(target):
        json_response(handler, 400, {"error": "Valid file path required"})
        return
    if not patch_content.strip():
        json_response(handler, 400, {"error": "patch content required"})
        return

    # Do NOT strip this. A unified diff must end with a newline; stripping the
    # trailing one makes patch report "unexpectedly ends in middle of line"
    # and refuse the diff, so every patch failed with "malformed patch" no
    # matter how valid it was.
    if not patch_content.endswith("\n"):
        patch_content += "\n"

    # Patching is writing. Same gate as /write: a patch applied to
    # ~/.ssh/authorized_keys or ~/.termux/boot/ is persistence, not editing.
    if is_sensitive_path(target) and not data.get("confirmed"):
        json_response(handler, 200, {
            "status": "confirmation_required",
            "path": target,
            "risk_level": "sensitive",
            "blocked": False,
            "requires_confirmation": True,
            "message": (
                f"Patching {target} can grant persistent access to this "
                "device. Re-send with confirmed: true to proceed."
            ),
        })
        return

    import base64
    safe_file = shell_quote(target)
    encoded = base64.b64encode(patch_content.encode()).decode()
    # $TMPDIR, not /tmp: the Android /tmp exists but is mode 0771 owned by
    # `shell`, so this process cannot write to it and every patch failed with
    # "Permission denied" before the diff was even read.
    diff_file = shell_quote(os.path.join(tmp_dir(), "_mcp_patch.diff"))

    # Safety: keep the pre-patch version before the file is modified.
    snap = snapshot_before_write(target, tool="patch")
    prefix = f"echo {shell_quote(f'[snapshot: {snap}]')}; " if snap else ""

    # The diff arrives on stdin rather than in the command string: the whole
    # command is a single argument to `sh -c`, capped at MAX_ARG_STRLEN
    # (128 KB), and base64 inflates by 4/3.
    cmd = prefix + (
        f'base64 -d > {diff_file} && '
        f'patch {safe_file} {diff_file} 2>&1 && '
        f'echo {shell_quote("Patch applied to " + target)} || '
        f'echo "Patch failed - check the diff format"'
    )
    execute_streaming(handler, cmd, stdin_data=encoded)


def handle_health(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    cmd = (
        'echo "═══════════════════════════════════";'
        'echo "   🔍 Termux Health Check";'
        'echo "═══════════════════════════════════";'
        'echo "";'
        'echo "📦 Core packages:";'
        'for pkg in python git curl wget tar gzip openssh; do'
        '  dpkg -s "$pkg" 2>/dev/null | grep -q "Status: install ok" && echo "  ✅ $pkg" || echo "  ❌ $pkg (pkg install $pkg)";'
        'done;'
        'echo "";'
        'echo "📱 Termux:API:";'
        'pm list packages 2>/dev/null | grep -q com.termux.api && echo "  ✅ Termux:API app installed" || echo "  ❌ Termux:API app NOT installed (get from F-Droid)";'
        'pkg list-installed 2>/dev/null | grep -q termux-api && echo "  ✅ termux-api package" || echo "  ❌ termux-api package (pkg install termux-api)";'
        'echo "";'
        'echo "💾 Storage:";'
        'df -h /data 2>/dev/null | awk "NR==2{printf \\"  %s used / %s total (%.0f%%)\\n\\", \\$3, \\$2, \\$5}" || echo "  ❌ Cannot read storage";'
        'echo "";'
        'echo "🌐 Network:";'
        'ping -c 1 -W 2 google.com >/dev/null 2>&1 && echo "  ✅ Internet: connected" || echo "  ⚠️ Internet: unreachable";'
        'echo "";'
        'echo "🔐 Permissions:";'
        'for perm in storage camera location microphone sms; do'
        '  case "$perm" in'
        '    storage) test -r /sdcard/ 2>/dev/null && echo "  ✅ Storage" || echo "  ❌ Storage (termux-setup-storage)";;'
        '    camera) pm list packages 2>/dev/null | grep -q com.termux.api && echo "  ✅ Camera (via API)" || echo "  ❌ Camera (needs Termux:API)";;'
        '    location) pm list packages 2>/dev/null | grep -q com.termux.api && echo "  ✅ Location (via API)" || echo "  ❌ Location (needs Termux:API)";;'
        '    *) echo "  — $perm: check Android Settings";;'
        '  esac;'
        'done;'
        'echo "";'
        'echo "🖥️ MCP Server:";'
        'echo "  ✅ Running on port ${TERMUX_MCP_PORT:-8080}";'
        'echo "";'
        'echo "═══════════════════════════════════";'
        'echo "Health check complete."'
    )
    execute_streaming(handler, cmd)


def handle_cloud_sync(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    action = data.get("action", "backup").strip()

    if action == "backup":
        output = data.get("output", f"termux_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz").strip()
        safe_out = shell_quote(output)
        target = data.get("target", "home").strip()
        cmd = (f'echo {shell_quote("Creating cloud backup: " + output)}; '
               f'echo "---"; cd {shell_quote(HOME)} && ')
        if target == "home":
            cmd += (
                f'tar -czf {safe_out} . --exclude=".cache" --exclude="__pycache__" '
                f'--exclude="node_modules" --exclude="*.pyc" 2>&1 | tail -5 && '
            )
        elif target == "packages":
            cmd += f'pkg list-installed > {safe_out} 2>&1 && '
        elif target == "configs":
            cmd += f'tar -czf {safe_out} .bashrc .zshrc .termux/ .config/ 2>/dev/null && '
        cmd += (
            f'echo "---"; echo {shell_quote("Backup created: " + output)}; ls -lh {safe_out};'
            f'echo ""; echo {shell_quote("To upload: pkg install rclone && rclone copy " + output + " remote:termux-backups/")}'
        )
    elif action == "restore":
        backup_file = data.get("file", "").strip()
        if not backup_file:
            execute_streaming(handler, 'echo "Specify backup file to restore"')
            return
        safe_file = shell_quote(backup_file)
        cmd = (
            f'echo {shell_quote("Restoring from: " + backup_file)};'
            f'tar -xzf {safe_file} -C {shell_quote(HOME)} 2>&1 && '
            f'echo "Restore complete" || echo "Restore failed"'
        )
    else:
        cmd = (
            'echo "Available backups:";'
            f'ls -lh {shell_quote(HOME)}/*.tar.gz 2>/dev/null || echo "No local backups found"'
        )
    execute_streaming(handler, cmd)


def handle_git_pr(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    action = data.get("action", "list").strip()
    repo = data.get("repo", "").strip()
    number = str(data.get("number", "")).strip()
    # The PR number is interpolated unquoted in four branches below; empty
    # stays empty so the per-branch "PR number required" checks still fire.
    try:
        safe_number = require_int(number) if number else ""
    except ValueError:
        json_response(handler, 400, {"error": "Valid PR number required"})
        return
    flags = f" --repo {shell_quote(repo)}" if repo else ""

    if action == "list":
        state = data.get("state", "open").strip()
        if state not in PR_STATES:
            json_response(handler, 400, {
                "error": f"state must be one of: {', '.join(PR_STATES)}"})
            return
        limit = require_int(data.get("limit", 10))
        cmd = (
            f'echo {shell_quote("PRs (" + state + "):")};'
            f'gh pr list --state {state} --limit {limit} {flags} 2>&1 || echo "Install: pkg install gh && gh auth login"'
        )
    elif action == "view":
        if not number: json_response(handler, 400, {"error": "PR number required"}); return
        cmd = f'gh pr view {safe_number} {flags} 2>&1 || echo "PR #{safe_number} not found"'
    elif action == "diff":
        if not number: json_response(handler, 400, {"error": "PR number required"}); return
        cmd = f'gh pr diff {safe_number} {flags} 2>&1 | head -300 || echo "Cannot show diff"'
    elif action == "merge":
        if not number: json_response(handler, 400, {"error": "PR number required"}); return
        method = data.get("method", "merge").strip()
        if method not in PR_MERGE_METHODS:
            json_response(handler, 400, {
                "error": f"method must be one of: {', '.join(PR_MERGE_METHODS)}"})
            return
        cmd = f'echo {shell_quote("Merging PR #" + safe_number + "...")}; gh pr merge {safe_number} --{method} {flags} 2>&1 || echo "Merge failed"'
    elif action == "approve":
        if not number: json_response(handler, 400, {"error": "PR number required"}); return
        cmd = f'gh pr review {safe_number} --approve {flags} 2>&1 || echo "Approve failed"'
    elif action == "status":
        cmd = f'echo "PR Status:"; gh pr status {flags} 2>&1 || echo "No PRs or gh not configured"'
    elif action == "create":
        title = data.get("title", "").strip()
        if not title: json_response(handler, 400, {"error": "PR title required"}); return
        body = data.get("body", "").strip()
        draft = " --draft" if data.get("draft", False) else ""
        cmd = f'echo {shell_quote("Creating: " + title + "...")}; gh pr create --title {shell_quote(title)} --body {shell_quote(body or "")} --base {shell_quote(data.get("base","main").strip())}{draft} {flags} 2>&1 || echo "Create failed"'
    else:
        cmd = 'echo "Actions: list view diff merge approve status create"'
    execute_streaming(handler, cmd)


RECIPES_FILE = os.path.join(HOME, ".termux_recipes.json")

DEFAULT_RECIPES = {
    "deploy-python-api": {
        "name": "Deploy Python API",
        "desc": "Install Python, create venv, setup Flask, start server",
        "steps": [
            "pkg install python python-pip -y",
            "cd ~ && mkdir -p api-project && cd api-project",
            "python3 -m venv .venv && source .venv/bin/activate",
            "pip install flask gunicorn",
            "echo 'from flask import Flask; app = Flask(__name__); @app.route(\"/\") def hello(): return {\"status\":\"ok\"}; app.run(host=\"0.0.0.0\",port=8080)' > app.py",
            "echo 'Ready! Run: cd ~/api-project && source .venv/bin/activate && python app.py'"
        ]
    },
    "setup-dev-env": {
        "name": "Setup Dev Environment",
        "desc": "Install git, python, node, vim, tmux, create project structure",
        "steps": [
            "pkg install git python nodejs vim tmux openssh -y",
            "mkdir -p ~/projects ~/scripts ~/backups",
            "git config --global init.defaultBranch main",
            "echo 'alias g=git' >> ~/.bashrc",
            "echo 'alias py=python3' >> ~/.bashrc",
            "echo 'Dev environment ready. Projects folder: ~/projects'"
        ]
    },
    "backup-everything": {
        "name": "Backup Everything",
        "desc": "Backup home, packages list, and configs with timestamp",
        "steps": [
            "cd ~ && mkdir -p ~/storage/shared/backups",
            "tar -czf ~/storage/shared/backups/home_$(date +%Y%m%d_%H%M%S).tar.gz . --exclude='.cache' --exclude='__pycache__' --exclude='node_modules'",
            "pkg list-installed > ~/storage/shared/backups/packages_$(date +%Y%m%d_%H%M%S).txt",
            "echo 'Backup complete. Files in ~/storage/shared/backups/'"
        ]
    },
    "system-audit": {
        "name": "System Audit",
        "desc": "Full system check: packages, storage, network, security",
        "steps": [
            "echo '=== Packages ===' && pkg list-installed | wc -l && echo 'packages installed'",
            "echo '=== Storage ===' && df -h /data",
            "echo '=== Memory ===' && free -h",
            "echo '=== Network ===' && ping -c 2 google.com",
            "echo '=== Listening Ports ===' && netstat -tlnp 2>/dev/null || ss -tlnp",
            "echo '=== Large Files ===' && find ~ -type f -size +10M 2>/dev/null | head -10",
            "echo 'Audit complete.'"
        ]
    },
}


def _load_recipes() -> dict:
    import json
    try:
        with open(RECIPES_FILE) as f:
            return json.load(f)
    except Exception:
        _save_recipes(DEFAULT_RECIPES)
        return dict(DEFAULT_RECIPES)


def _save_recipes(data: dict) -> None:
    import json
    os.makedirs(os.path.dirname(RECIPES_FILE) or ".", exist_ok=True)
    with open(RECIPES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def handle_recipe_list(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    recipes = _load_recipes()
    lines = ["Available Recipes:", "---"]
    for key, r in recipes.items():
        lines.append(f"  {key} - {r['name']}: {r['desc']}")
    output = "\n".join(lines) + "\n"
    body = output.encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_recipe_run(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    recipe_id = data.get("recipe", "").strip()
    recipes = _load_recipes()
    recipe = recipes.get(recipe_id) or recipes.get(list(recipes.keys())[0]) if recipes else None
    if not recipe:
        json_response(handler, 404, {"error": f"Recipe '{recipe_id}' not found"})
        return
    # A recipe step *is* a command, so only the progress line is quoted —
    # quoting the step itself would turn every recipe into a no-op echo.
    cmd = " && ".join([f'echo {shell_quote(f"> {s}")} && {s}' for s in recipe["steps"]])
    execute_streaming(handler, cmd)


def handle_recipe_save(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    recipe_id = data.get("recipe", "").strip()
    name = data.get("name", "").strip()
    desc = data.get("desc", "").strip()
    steps = data.get("steps", [])
    if not recipe_id or not name or not steps:
        json_response(handler, 400, {"error": "recipe, name, and steps required"})
        return
    recipes = _load_recipes()
    recipes[recipe_id] = {"name": name, "desc": desc, "steps": steps}
    _save_recipes(recipes)
    json_response(handler, 200, {"saved": recipe_id, "total": len(recipes)})


CONTEXT_FILE = os.path.join(HOME, ".termux_context.json")


def handle_context(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    import json
    try:
        with open(CONTEXT_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {"note": "No context saved yet. Run context-save first."}
    json_response(handler, 200, data)


def handle_context_save(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    import json, time
    context = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": os.popen("hostname 2>/dev/null || echo termux").read().strip(),
        "packages_count": os.popen("pkg list-installed 2>/dev/null | wc -l").read().strip(),
        "python_version": os.popen("python3 --version 2>/dev/null || echo none").read().strip(),
        "disk_used": os.popen("df -h /data 2>/dev/null | awk 'NR==2{print $3,$4,$5}' || echo unknown").read().strip(),
        "ram": os.popen("free -h 2>/dev/null | awk '/Mem:/{print $3,$2}' || echo unknown").read().strip(),
        "current_dir": get_current_dir(),
    }
    try:
        with open(CONTEXT_FILE, "w") as f:
            json.dump(context, f, indent=2)
        json_response(handler, 200, {"saved": context})
    except Exception as e:
        json_response(handler, 500, {"error": str(e)})
