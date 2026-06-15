import os
import time
from typing import TYPE_CHECKING

from ..shell import execute_streaming, get_current_dir
from ..utils import shell_quote, is_safe_path, json_response

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")


def handle_system_info(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    cmd = (
        "python3 -c \"import os,json;"
        "cpu=os.popen('top -bn1 2>/dev/null|grep CPU:').read();"
        "cpu_pct=cpu.split()[1].replace('%','') if cpu else '0';"
        "ram=os.popen('free -m 2>/dev/null|grep Mem:').read().split();"
        "ram_total=ram[1] if len(ram)>1 else '0';"
        "ram_used=ram[2] if len(ram)>2 else '0';"
        "disk=os.popen('df -m /data 2>/dev/null').readlines();"
        "disk_parts=disk[1].split() if len(disk)>1 else ['0','0','0'];"
        "t=0;"
        "try:\n t=int(open('/sys/class/thermal/thermal_zone0/temp').read().strip())//1000\n"
        "except: pass;"
        "u=0;"
        "try:\n u=int(float(open('/proc/uptime').read().split()[0]))\n"
        "except: pass;"
        "print(json.dumps({'cpu_percent':cpu_pct.strip(),'ram_mb_total':ram_total,'ram_mb_used':ram_used,'disk_mb_total':disk_parts[1],'disk_mb_used':disk_parts[2],'temp_celsius':t,'uptime_seconds':u}))\""
    )
    execute_streaming(handler, cmd)


def handle_process_list(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    limit = str(data.get("limit", 20))
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    cmd = (
        f'echo "PID USER CPU% MEM% COMMAND";'
        f'ps aux --sort=-%cpu 2>/dev/null | head -n {limit} | '
        f'awk \'{{printf "%-8s %-8s %-5s %-5s %s\\n", $2, $1, $3, $4, substr($0, index($0,$11))}}\''
    )
    execute_streaming(handler, cmd)


def handle_process_kill(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    pid = str(data.get("pid", "")).strip()
    signal_num = str(data.get("signal", "15")).strip()

    if not pid or not pid.isdigit():
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

    cmd = (
        f'echo "Adding cron job: {label}";'
        f'(crontab -l 2>/dev/null; echo "# {label}";'
        f'echo "{schedule} {command}") | crontab - 2>&1 && '
        f'echo "Cron job added: {schedule} {command}" '
        f'|| echo "Failed. Install: pkg install cronie termux-services"'
    )
    execute_streaming(handler, cmd)


def handle_cron_list(handler: "BaseHTTPRequestHandler", _data: dict) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    cmd = (
        'echo "Cron Jobs:";'
        'echo "---";'
        'crontab -l 2>&1 || echo "No cron jobs. Install: pkg install cronie termux-services"'
    )
    execute_streaming(handler, cmd)


def handle_cron_remove(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    label = data.get("label", "").strip()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    if label:
        safe = shell_quote(label)
        cmd = (
            f'crontab -l 2>/dev/null | grep -v "{label}" | crontab - 2>&1 && '
            f'echo "Removed cron jobs matching: {label}" '
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
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    if file2:
        if not is_safe_path(file2):
            json_response(handler, 400, {"error": "Invalid file2 path"})
            return
        safe2 = shell_quote(file2)
        cmd = f'diff -u {safe1} {safe2} 2>&1 || echo "(files differ or one missing)"'
    else:
        cmd = (
            f'echo "File: {file1}";'
            f'wc -l {safe1} 2>/dev/null;'
            f'echo "Last modified: $(stat -c %y {safe1} 2>/dev/null)";'
            f'echo "Size: $(stat -c %s {safe1} 2>/dev/null) bytes"'
        )
    execute_streaming(handler, cmd)


def handle_patch(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    target = data.get("file", "").strip()
    patch_content = data.get("patch", "").strip()

    if not target or not is_safe_path(target):
        json_response(handler, 400, {"error": "Valid file path required"})
        return
    if not patch_content:
        json_response(handler, 400, {"error": "patch content required"})
        return

    import base64
    safe_file = shell_quote(target)
    encoded = base64.b64encode(patch_content.encode()).decode()

    cmd = (
        f'echo {shell_quote(encoded)} | base64 -d > /tmp/_mcp_patch.diff && '
        f'patch {safe_file} /tmp/_mcp_patch.diff 2>&1 && '
        f'echo "Patch applied to {target}" || '
        f'echo "Patch failed - check the diff format"'
    )
    execute_streaming(handler, cmd)


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
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    if action == "backup":
        output = data.get("output", f"termux_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz").strip()
        safe_out = shell_quote(output)
        target = data.get("target", "home").strip()
        cmd = f'echo "Creating cloud backup: {output}"; echo "---"; cd {shell_quote(HOME)} && '
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
            f'echo "---"; echo "Backup created: {output}"; ls -lh {safe_out};'
            f'echo ""; echo "To upload: pkg install rclone && rclone copy {output} remote:termux-backups/"'
        )
    elif action == "restore":
        backup_file = data.get("file", "").strip()
        if not backup_file:
            execute_streaming(handler, 'echo "Specify backup file to restore"')
            return
        safe_file = shell_quote(backup_file)
        cmd = (
            f'echo "Restoring from: {backup_file}";'
            f'tar -xzf {safe_file} -C {shell_quote(HOME)} 2>&1 && '
            f'echo "Restore complete" || echo "Restore failed"'
        )
    else:
        cmd = (
            'echo "Available backups:";'
            f'ls -lh {shell_quote(HOME)}/*.tar.gz 2>/dev/null || echo "No local backups found"'
        )
    execute_streaming(handler, cmd)
