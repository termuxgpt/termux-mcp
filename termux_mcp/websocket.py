import base64
import hashlib
import json
import os
import signal
import struct
import subprocess
import threading
import time
from typing import Optional

from .config import AUTO_INPUT_INTERVAL, COMMAND_TIMEOUT, HOME
from .utils import is_install_command, shell_quote, is_safe_path, encode_base64

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_current_dir = os.getcwd()
_active_pid: Optional[int] = None
_pid_lock = threading.Lock()
_ws_session = {"name": None, "created": False}


def set_cwd(path: str) -> None:
    global _current_dir
    _current_dir = path


def get_cwd() -> str:
    return _current_dir


def _make_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    frame = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        frame += bytes([length])
    elif length < 65536:
        frame += bytes([126]) + struct.pack(">H", length)
    else:
        frame += bytes([127]) + struct.pack(">Q", length)
    return frame + payload


def _read_frame(sock) -> tuple:
    b1 = sock.recv(1)
    if not b1:
        return None, None
    b2 = sock.recv(1)
    if not b2:
        return None, None
    opcode = b1[0] & 0x0F
    length = b2[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    masks = sock.recv(4)
    data = bytearray(sock.recv(length))
    for i in range(length):
        data[i] ^= masks[i % 4]
    return opcode, bytes(data)


def _do_handshake(sock, headers: str) -> bool:
    for line in headers.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + WS_MAGIC.decode()).encode()).digest()
            ).decode()
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            sock.sendall(response.encode())
            return True
    return False


def handle_cd(raw_cmd: str) -> tuple:
    rest = raw_cmd[2:].strip()
    path_part = rest
    for sep in (";", "&&"):
        idx = rest.find(sep)
        if idx != -1:
            path_part = rest[:idx].strip()
            break
    if not path_part or path_part == "~":
        set_cwd(HOME)
        return True, HOME
    raw_path = path_part.strip().replace("~", HOME, 1)
    new_path = os.path.abspath(
        raw_path if os.path.isabs(raw_path) else os.path.join(get_cwd(), raw_path)
    )
    if os.path.isdir(new_path):
        set_cwd(new_path)
        return True, get_cwd()
    return False, f"Directory not found: {new_path}"


def _spawn_auto_input(process: subprocess.Popen, cmd: str) -> None:
    if not is_install_command(cmd):
        return

    def _worker() -> None:
        try:
            while process.poll() is None:
                time.sleep(AUTO_INPUT_INTERVAL)
                try:
                    process.stdin.write("y\n")
                    process.stdin.flush()
                except Exception:
                    break
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ── WebSocket tool executor ───────────────────────────────────────────────────

def _ws_run_process(sock, raw_cmd: str) -> None:
    """Execute a command and stream output back via WebSocket frames."""
    global _active_pid
    raw_cmd = raw_cmd.strip()

    if raw_cmd.startswith("cd"):
        ok, msg = handle_cd(raw_cmd)
        rest = raw_cmd[2:].strip()
        for sep in (";", "&&"):
            idx = rest.find(sep)
            if idx != -1:
                chained = rest[idx + len(sep):].strip()
                if chained and ok:
                    sock.sendall(_make_frame(f"cd: {msg}\n".encode()))
                    raw_cmd = chained
                    break
        else:
            sock.sendall(_make_frame(f"{msg}\n".encode()))
            return

    cmd = f"export DEBIAN_FRONTEND=noninteractive; {raw_cmd}"
    process = None
    killed = threading.Event()

    try:
        kwargs = dict(shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      stdin=subprocess.PIPE, text=True, cwd=get_cwd())
        if hasattr(os, "setsid"):
            kwargs["preexec_fn"] = os.setsid
        process = subprocess.Popen(f"export PAGER=cat; {cmd}", **kwargs)

        with _pid_lock:
            _active_pid = process.pid
        _spawn_auto_input(process, raw_cmd)

        def _timeout_watchdog() -> None:
            try:
                process.wait(timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                killed.set()
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                        time.sleep(1)
                    process.kill()
                except Exception:
                    process.kill()

        watchdog = threading.Thread(target=_timeout_watchdog, daemon=True)
        watchdog.start()

        for line in process.stdout:
            try:
                sock.sendall(_make_frame(line.encode()))
            except Exception:
                break
            if killed.is_set():
                try:
                    sock.sendall(_make_frame(f"\nTimed out after {COMMAND_TIMEOUT}s\n".encode()))
                except Exception:
                    pass
                break

        watchdog.join(timeout=2)
        if not killed.is_set():
            tag = "Done" if process.returncode == 0 else f"Exit: {process.returncode}"
            try:
                sock.sendall(_make_frame(f"\n{tag}\n".encode()))
            except Exception:
                pass
    except Exception as e:
        try:
            sock.sendall(_make_frame(f"\nError: {e}\n".encode()))
        except Exception:
            pass
    finally:
        with _pid_lock:
            _active_pid = None


def _ws_send_json(sock, data: dict) -> None:
    sock.sendall(_make_frame(json.dumps(data).encode()))


def _ws_execute_tool(sock, tool: str, params: dict) -> None:
    """Execute any tool and stream output via WebSocket."""
    p = params or {}

    if tool == "run":
        _ws_run_process(sock, p.get("cmd", ""))
        return

    # Build and execute command for each tool type
    cmd = None

    if tool == "ls":
        path = p.get("path") or "."
        detailed = p.get("detailed", False)
        flags = "-la" if detailed else "-1"
        if is_safe_path(path):
            cmd = f'ls {flags} {shell_quote(path)} 2>/dev/null || echo Cannot access: {shell_quote(path)}'
        else:
            _ws_send_json(sock, {"error": "Path not allowed"})
            return

    elif tool == "read":
        path = p.get("path", "")
        if not path:
            _ws_send_json(sock, {"error": "Missing path"})
            return
        if not is_safe_path(path):
            _ws_send_json(sock, {"error": "Path not allowed"})
            return
        cmd = f'head -n 500 {shell_quote(path)} 2>/dev/null || echo Cannot read: {shell_quote(path)}'

    elif tool == "write":
        path = p.get("path", "")
        content = p.get("content", "")
        if not path:
            _ws_send_json(sock, {"error": "Missing path"})
            return
        if not is_safe_path(path):
            _ws_send_json(sock, {"error": "Path not allowed"})
            return
        encoded = encode_base64(content)
        cmd = (f'mkdir -p "$(dirname {shell_quote(path)})" 2>/dev/null; '
               f'echo {shell_quote(encoded)} | base64 -d > {shell_quote(path)} && '
               f'echo Written: {shell_quote(path)}')

    elif tool == "mkdir":
        path = p.get("path", "")
        if not path or not is_safe_path(path):
            _ws_send_json(sock, {"error": "Invalid path"})
            return
        cmd = f'mkdir -p {shell_quote(path)} && echo Created: {shell_quote(path)}'

    elif tool == "delete":
        path = p.get("path", "")
        recursive = p.get("recursive", False)
        if not path or not is_safe_path(path):
            _ws_send_json(sock, {"error": "Invalid path"})
            return
        flags = "-rf" if recursive else ""
        cmd = f'rm {flags} {shell_quote(path)} 2>/dev/null && echo Deleted: {shell_quote(path)} || echo Failed to delete: {shell_quote(path)}'

    elif tool == "search":
        path = p.get("path") or "."
        pattern = p.get("pattern") or p.get("query") or p.get("name") or "*"
        if not is_safe_path(path):
            _ws_send_json(sock, {"error": "Path not allowed"})
            return
        cmd = f'find {shell_quote(path)} -name {shell_quote(pattern)} -type f 2>/dev/null | head -n 30'

    elif tool == "cancel":
        ok = cancel_active()
        _ws_send_json(sock, {"cancelled": ok})
        return

    # Device tools
    elif tool == "screenshot":
        output = p.get("output", "")
        cmd = f"termux-screenshot {'-o ' + shell_quote(output) if output else ''} 2>/dev/null || echo Screenshot failed"

    elif tool == "camera":
        camera_id = str(p.get("camera_id", 0))
        output = p.get("output", "/sdcard/DCIM/termux_photo.jpg")
        cmd = f"termux-camera-photo -c {camera_id} {shell_quote(output)} 2>/dev/null || echo Camera photo failed"

    elif tool == "battery":
        cmd = "termux-battery-status 2>/dev/null || echo '{}'"

    elif tool == "location":
        provider = p.get("provider", "gps")
        cmd = f"termux-location -p {provider} -r last 2>/dev/null || echo '{{}}'"

    elif tool == "wifi":
        cmd = "termux-wifi-connectioninfo 2>/dev/null || echo '{}'"

    elif tool == "clipboard_get":
        cmd = "termux-clipboard-get 2>/dev/null || echo '(clipboard empty)'"

    elif tool == "clipboard_set":
        text = p.get("text", "")
        if not text:
            _ws_send_json(sock, {"error": "Missing text"})
            return
        cmd = f"echo {shell_quote(text)} | termux-clipboard-set && echo 'Clipboard set' || echo Failed"

    # Communication
    elif tool == "notify":
        title = p.get("title", "TermuxGPT")
        content = p.get("content", "")
        if not content:
            _ws_send_json(sock, {"error": "Missing content"})
            return
        cmd = f"termux-notification --title {shell_quote(title)} --content {shell_quote(content)} 2>/dev/null && echo 'Notification sent' || echo 'Notification failed'"

    elif tool == "sms":
        number = p.get("number", "")
        text = p.get("text", "")
        if not number or not text:
            _ws_send_json(sock, {"error": "Missing number or text"})
            return
        cmd = f"termux-sms-send -n {shell_quote(number)} {shell_quote(text)} 2>/dev/null && echo 'SMS sent' || echo 'SMS failed'"

    elif tool == "tts":
        text = p.get("text", "")
        if not text:
            _ws_send_json(sock, {"error": "Missing text"})
            return
        cmd = f"termux-tts-speak {shell_quote(text)} 2>/dev/null && echo 'Spoken' || echo 'TTS failed'"

    elif tool == "toast":
        text = p.get("text", "")
        if not text:
            _ws_send_json(sock, {"error": "Missing text"})
            return
        cmd = f"termux-toast {shell_quote(text)} 2>/dev/null && echo 'Toast shown' || echo 'Toast failed'"

    elif tool == "share":
        text = p.get("text", "")
        file = p.get("file", "")
        if file and is_safe_path(file):
            cmd = f"termux-share -a send {shell_quote(file)} 2>/dev/null || echo 'Share failed'"
        elif text:
            cmd = (f"echo {shell_quote(text)} > /data/data/com.termux/files/usr/tmp/_ws_share.txt 2>/dev/null && "
                   f"termux-share -a send /data/data/com.termux/files/usr/tmp/_ws_share.txt 2>/dev/null && "
                   f"echo 'Share opened' || echo 'Share failed'")
        else:
            _ws_send_json(sock, {"error": "Missing text or file"})
            return

    # Smart
    elif tool == "smart_install":
        packages = p.get("packages", "")
        if not packages:
            _ws_send_json(sock, {"error": "Missing packages"})
            return
        cmd = f"pkg install -y {packages} 2>&1 | tail -30"

    elif tool == "diagnose":
        intent = p.get("intent", "all")
        checks = ['echo "=== Diagnose: ' + intent + ' ==="']
        if intent in ("python", "all"):
            checks += ['python3 --version 2>&1 || echo "Missing python3"',
                       'pip --version 2>&1 || echo "Missing pip"']
        if intent in ("git", "all"):
            checks += ['git --version 2>&1 || echo "Missing git"']
        if intent in ("storage", "all"):
            checks += ['ls -la ~/storage/ 2>&1 || echo "Storage not setup"',
                       'df -h /data 2>/dev/null || echo "Cannot check disk"']
        cmd = " && ".join(checks)

    elif tool == "optimize":
        cmd = ('echo "=== Performance ==="; free -h 2>/dev/null; echo "---"; '
               'df -h /data 2>/dev/null; echo "---"; '
               'ps aux --sort=-%mem 2>/dev/null | head -8')

    # Network
    elif tool == "download":
        url = p.get("url", "")
        if not url:
            _ws_send_json(sock, {"error": "Missing url"})
            return
        cmd = f"termux-download {shell_quote(url)} 2>/dev/null && echo 'Download started' || echo 'Download failed'"

    elif tool == "public_ip":
        cmd = "curl -s https://api.ipify.org 2>/dev/null || curl -s https://ifconfig.me 2>/dev/null || echo 'No internet'"

    elif tool == "weather":
        city = p.get("city", "")
        cmd = f"curl -s wttr.in/{shell_quote(city)}?format=3 2>/dev/null || echo 'Install curl: pkg install curl'" if city else "curl -s 'wttr.in/?format=3' 2>/dev/null"

    elif tool == "speedtest":
        cmd = "speedtest-cli --simple 2>/dev/null || echo 'Install: pkg install speedtest-cli'"

    # Media
    elif tool == "qrcode":
        text = p.get("text", "")
        output = p.get("output", "qr.png")
        if not text:
            _ws_send_json(sock, {"error": "Missing text"})
            return
        cmd = f"qrencode -o {shell_quote(output)} {shell_quote(text)} 2>/dev/null && echo 'QR saved to {output}' || echo 'Install: pkg install qrencode'"

    elif tool == "image_process":
        action = p.get("action", "info")
        inp = p.get("input", "")
        out = p.get("output", "")
        if not inp:
            _ws_send_json(sock, {"error": "Missing input"})
            return
        if action == "info":
            cmd = f"identify -verbose {shell_quote(inp)} 2>/dev/null || echo 'Install: pkg install imagemagick'"
        elif action == "resize" and out:
            w, h = p.get("width", 800), p.get("height", 600)
            cmd = f"convert {shell_quote(inp)} -resize {w}x{h}! {shell_quote(out)} 2>/dev/null && echo 'Resized' || echo 'Failed'"
        else:
            cmd = f"echo 'Action: {action} on {inp}'"

    elif tool == "ocr":
        inp = p.get("input", "")
        if not inp:
            _ws_send_json(sock, {"error": "Missing input"})
            return
        cmd = f"tesseract {shell_quote(inp)} stdout 2>/dev/null || echo 'Install: pkg install tesseract'"

    # Monitor & Manage
    elif tool == "system_info":
        cmd = ('cpu=$(top -bn1 2>/dev/null | grep -oP "[0-9.]+%" | head -1 | tr -d "%" || echo 0);'
               'ram_total=$(free -m 2>/dev/null | awk "/Mem:/{print \$2}" || echo 0);'
               'ram_used=$(free -m 2>/dev/null | awk "/Mem:/{print \$3}" || echo 0);'
               'disk_total=$(df -m /data 2>/dev/null | awk "END{print \$2}" || echo 0);'
               'disk_used=$(df -m /data 2>/dev/null | awk "END{print \$3}" || echo 0);'
               'echo "{\\"cpu_percent\\":\\"$cpu\\",\\"ram_mb_total\\":$ram_total,\\"ram_mb_used\\":$ram_used,\\"disk_mb_total\\":$disk_total,\\"disk_mb_used\\":$disk_used}"')

    elif tool == "process_list":
        limit = p.get("limit", 20)
        cmd = f'ps aux --sort=-%cpu 2>/dev/null | head -n {limit}'

    elif tool == "process_kill":
        pid = p.get("pid", "")
        if not pid:
            _ws_send_json(sock, {"error": "Missing pid"})
            return
        cmd = f"kill -15 {pid} 2>&1 && echo 'Process {pid} terminated' || echo 'Failed to kill {pid}'"

    elif tool == "health":
        cmd = ('echo "=== Termux Health Check ==="; '
               'echo "Python: $(python3 --version 2>&1)"; '
               'echo "Pip: $(pip --version 2>&1 | head -1)"; '
               'echo "Git: $(git --version 2>&1)"; '
               'echo "Storage: $(df -h /data 2>/dev/null | tail -1)"; '
               'ping -c 1 -W 2 google.com >/dev/null 2>&1 && echo "Internet: OK" || echo "Internet: UNREACHABLE"')

    # Cron & Backup
    elif tool == "cron_add":
        schedule = p.get("schedule", "")
        command = p.get("command", "")
        label = p.get("label", "task")
        if not schedule or not command:
            _ws_send_json(sock, {"error": "Missing schedule or command"})
            return
        cmd = (f'(crontab -l 2>/dev/null; echo "# {label}"; '
               f'echo "{schedule} {command}") | crontab - 2>&1 && '
               f'echo "Cron added: {label}" || echo "Failed - install: pkg install cronie"')

    elif tool == "cron_list":
        cmd = 'echo "Cron Jobs:"; crontab -l 2>&1 || echo "No cron jobs"'

    elif tool == "cron_remove":
        label = p.get("label", "")
        if label:
            cmd = f'crontab -l 2>/dev/null | grep -v "{label}" | crontab - 2>&1 && echo "Removed: {label}" || echo "Failed"'
        else:
            cmd = 'crontab -r 2>&1 && echo "All cron jobs removed" || echo "No cron jobs"'

    elif tool == "backup":
        target = p.get("target", "home")
        output = p.get("output", "")
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = output or f"~/storage/shared/termux_backup_{ts}.tar.gz"
        if target == "packages":
            cmd = f'pkg list-installed > {shell_quote(out_path)} 2>&1 && echo "Package list saved: {out_path}"'
        elif target == "configs":
            cmd = f'tar -czf {shell_quote(out_path)} ~/.bashrc ~/.zshrc ~/.termux/ ~/.config/ 2>/dev/null && echo "Configs saved: {out_path}" || echo "Backup failed"'
        else:
            cmd = (f'tar -czf {shell_quote(out_path)} -C {HOME} . --exclude=".cache" --exclude="__pycache__" '
                   f'--exclude="node_modules" 2>&1 && echo "Backup complete: {out_path}" || echo "Backup failed"')

    elif tool == "restore":
        file = p.get("file", "")
        target = p.get("target", "home")
        if not file:
            _ws_send_json(sock, {"error": "Missing file path"})
            return
        if target == "packages":
            cmd = f'xargs pkg install -y < {shell_quote(file)} 2>&1 | tail -20 && echo "Packages restored"'
        else:
            cmd = f'tar -xzf {shell_quote(file)} -C {HOME} 2>&1 && echo "Restore complete" || echo "Restore failed"'

    elif tool == "cloud_sync":
        action = p.get("action", "list")
        target = p.get("target", "home")
        if action == "backup":
            out = p.get("output") or f"termux_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
            cmd = f'tar -czf {shell_quote(out)} -C {HOME} . --exclude=".cache" 2>&1 && echo "Cloud backup: {out}" || echo "Backup failed"'
        elif action == "restore":
            f = p.get("file", "")
            cmd = f'tar -xzf {shell_quote(f)} -C {HOME} 2>&1 && echo "Restored from cloud" || echo "Restore failed"' if f else 'echo "Missing file"'
        else:
            cmd = f'ls -lh {HOME}/*.tar.gz 2>/dev/null || echo "No local backups"'

    # Git PR
    elif tool == "git_pr":
        action = p.get("action", "list")
        repo = p.get("repo", "")
        flags = f" --repo {shell_quote(repo)}" if repo else ""
        if action == "list":
            state = p.get("state", "open")
            limit = p.get("limit", 10)
            cmd = f'gh pr list --state {state} --limit {limit} {flags} 2>&1 || echo "Install: pkg install gh && gh auth login"'
        elif action == "status":
            cmd = f'gh pr status {flags} 2>&1 || echo "No PRs"'
        elif action == "view":
            num = p.get("number", "")
            cmd = f'gh pr view {num} {flags} 2>&1 || echo "PR not found"' if num else 'echo "Missing number"'
        elif action == "diff":
            num = p.get("number", "")
            cmd = f'gh pr diff {num} {flags} 2>&1 | head -300 || echo "Cannot show diff"' if num else 'echo "Missing number"'
        elif action == "merge":
            num = p.get("number", "")
            method = p.get("method", "merge")
            cmd = f'gh pr merge {num} --{method} {flags} 2>&1 || echo "Merge failed"' if num else 'echo "Missing number"'
        elif action == "approve":
            num = p.get("number", "")
            cmd = f'gh pr review {num} --approve {flags} 2>&1 || echo "Approve failed"' if num else 'echo "Missing number"'
        elif action == "create":
            title = p.get("title", "")
            body = p.get("body", "")
            base = p.get("base", "main")
            draft = " --draft" if p.get("draft", False) else ""
            cmd = f'gh pr create --title {shell_quote(title)} --body {shell_quote(body or "")} --base {shell_quote(base)}{draft} {flags} 2>&1 || echo "Create failed"' if title else 'echo "Missing title"'
        else:
            cmd = 'echo "Actions: list view diff merge approve status create"'

    # Recipes
    elif tool == "recipe_list":
        from .handlers.features import _load_recipes
        recipes = _load_recipes()
        lines = ["Recipes:"]
        for key, r in recipes.items():
            lines.append(f"  {key} - {r['name']}: {r['desc']}")
        _ws_send_json(sock, {"output": "\n".join(lines)})
        return

    elif tool == "recipe_run":
        from .handlers.features import _load_recipes
        recipe_id = p.get("recipe", "")
        recipes = _load_recipes()
        recipe = recipes.get(recipe_id)
        if not recipe:
            _ws_send_json(sock, {"error": f"Recipe '{recipe_id}' not found"})
            return
        cmd = " && ".join([f'echo "> {s}" && {s}' for s in recipe["steps"]])

    elif tool == "recipe_save":
        from .handlers.features import _load_recipes, _save_recipes
        recipe_id = p.get("recipe", "")
        name = p.get("name", "")
        desc = p.get("desc", "")
        steps = p.get("steps", [])
        if not recipe_id or not name or not steps:
            _ws_send_json(sock, {"error": "Missing recipe, name, or steps"})
            return
        recipes = _load_recipes()
        recipes[recipe_id] = {"name": name, "desc": desc, "steps": steps}
        _save_recipes(recipes)
        _ws_send_json(sock, {"saved": recipe_id, "total": len(recipes)})
        return

    # Context
    elif tool == "context":
        from .handlers.features import CONTEXT_FILE
        try:
            with open(CONTEXT_FILE) as f:
                ctx = json.load(f)
        except Exception:
            ctx = {"note": "No context saved yet"}
        _ws_send_json(sock, ctx)
        return

    elif tool == "context_save":
        from .handlers.features import CONTEXT_FILE
        ctx = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hostname": os.popen("hostname 2>/dev/null || echo termux").read().strip(),
            "packages_count": os.popen("pkg list-installed 2>/dev/null | wc -l").read().strip(),
            "python_version": os.popen("python3 --version 2>/dev/null || echo none").read().strip(),
            "disk_used": os.popen("df -h /data 2>/dev/null | awk 'NR==2{print $3,$4,$5}' || echo unknown").read().strip(),
            "current_dir": get_cwd(),
        }
        try:
            with open(CONTEXT_FILE, "w") as f:
                json.dump(ctx, f, indent=2)
            _ws_send_json(sock, {"saved": ctx})
        except Exception as e:
            _ws_send_json(sock, {"error": str(e)})
        return

    # History
    elif tool == "history":
        from .handlers.history import _load
        entries = _load()
        _ws_send_json(sock, {"entries": entries, "count": len(entries)})
        return

    elif tool == "history_save":
        raw_input = (p.get("rawInput") or "").strip()
        output = (p.get("output") or "").strip()
        if not raw_input and not output:
            _ws_send_json(sock, {"error": "Missing rawInput or output"})
            return
        from .handlers.history import _load, _save, MAX_ENTRIES
        entries = _load()
        entries.append({
            "rawInput": raw_input,
            "output": output[:5000] if len(output) > 5000 else output,
            "ranCommand": (p.get("ranCommand") or "").strip(),
            "success": p.get("success", True),
            "traces": p.get("traces") or [],
            "timestamp": time.time(),
        })
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _save(entries)
        _ws_send_json(sock, {"saved": True, "total": len(entries)})
        return

    elif tool == "history_clear":
        from .handlers.history import HISTORY_FILE
        try:
            os.remove(HISTORY_FILE)
            _ws_send_json(sock, {"cleared": True})
        except FileNotFoundError:
            _ws_send_json(sock, {"cleared": True})
        except Exception as e:
            _ws_send_json(sock, {"error": str(e)})
        return

    # Session (tmux)
    elif tool == "session_start":
        name = p.get("name", "termux-mcp")
        cmd = (f'tmux has-session -t {shell_quote(name)} 2>/dev/null && '
               f'echo "Session {name} already exists" || '
               f'(tmux new-session -d -s {shell_quote(name)} 2>&1 && echo "Session {name} created") || '
               f'echo "tmux not installed - pkg install tmux"')
        _ws_session["name"] = name
        _ws_session["created"] = True

    elif tool == "session_run":
        sess_name = p.get("session") or _ws_session.get("name") or "termux-mcp"
        cmd_to_run = p.get("cmd", "")
        if not cmd_to_run:
            _ws_send_json(sock, {"error": "Missing cmd"})
            return
        # Start session if not created
        sess_exists = os.popen(f'tmux has-session -t {shell_quote(sess_name)} 2>/dev/null && echo yes || echo no').read().strip()
        if sess_exists != "yes":
            os.system(f'tmux new-session -d -s {shell_quote(sess_name)} 2>/dev/null')
            _ws_session["name"] = sess_name
            _ws_session["created"] = True
        # Capture lines before command
        before = int(os.popen(f'tmux capture-pane -p -t {shell_quote(sess_name)} 2>/dev/null | wc -l').read().strip() or 0)
        # Send command
        os.system(f'tmux send-keys -t {shell_quote(sess_name)} {shell_quote(cmd_to_run)} Enter')
        time.sleep(0.3)
        # Capture output incrementally
        def _capture_loop():
            seen = before
            for _ in range(120):  # up to 60 seconds
                time.sleep(0.5)
                try:
                    output = os.popen(f'tmux capture-pane -p -t {shell_quote(sess_name)} 2>/dev/null').read()
                    lines = output.split('\n')
                    if len(lines) > seen:
                        new_lines = lines[seen:]
                        seen = len(lines)
                        for l in new_lines:
                            if l.strip():
                                sock.sendall(_make_frame((l + '\n').encode()))
                except Exception:
                    break
                # Check if prompt returned (command finished)
                pane = os.popen(f'tmux capture-pane -p -t {shell_quote(sess_name)} 2>/dev/null | tail -3').read()
                if '$' in pane or '#' in pane:
                    break
        _capture_loop()
        _ws_send_json(sock, {"output": "Command sent to session " + sess_name})
        return

    elif tool == "session_list":
        out = os.popen('tmux list-sessions 2>/dev/null || echo "No sessions (tmux not installed?)"').read().strip()
        _ws_send_json(sock, {"sessions": out})
        return

    elif tool == "session_kill":
        name = p.get("session") or _ws_session.get("name") or "termux-mcp"
        os.system(f'tmux kill-session -t {shell_quote(name)} 2>/dev/null')
        _ws_session["name"] = None
        _ws_session["created"] = False
        _ws_send_json(sock, {"killed": name})
        return

    else:
        _ws_send_json(sock, {"error": f"Unknown tool: {tool}"})
        return

    if cmd:
        _ws_run_process(sock, cmd)


# ── WebSocket handler ─────────────────────────────────────────────────────────


def ws_handler(sock, raw_headers: str) -> None:
    if not _do_handshake(sock, raw_headers):
        sock.close()
        return

    set_cwd(HOME)

    try:
        while True:
            opcode, data = _read_frame(sock)
            if opcode is None or opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                sock.sendall(_make_frame(data, OP_PONG))
                continue
            if opcode == OP_TEXT and data:
                try:
                    msg = json.loads(data.decode())
                except json.JSONDecodeError:
                    continue

                tool = msg.get("tool")
                params = msg.get("params", {})

                if tool:
                    # New generic tool routing
                    _ws_execute_tool(sock, tool, params)
                else:
                    # Backward compat: legacy {"cmd": "..."} messages
                    cmd = msg.get("cmd", "")
                    if cmd:
                        _ws_execute_tool(sock, "run", {"cmd": cmd})
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
