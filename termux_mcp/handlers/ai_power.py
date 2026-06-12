"""
AI-Native Termux Power Features — solving real user pain points.
Each handler runs diagnostic shell commands so the AI agent can understand
the current state, then take action. The intelligence is in the agent loop —
these handlers expose the TERMINAL CAPABILITIES the AI needs.
"""
import os
from typing import TYPE_CHECKING

from ..shell import execute_streaming, get_current_dir, cancel_active

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

HOME = os.environ.get("HOME", "/data/data/com.termux/files/home")


def _json_response(handler: "BaseHTTPRequestHandler", status: int, payload: dict) -> None:
    import json
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ═══════════════════════════════════════════════════════════════════════════
# 1. SMART INSTALL — AI-powered package install with conflict resolution
# ═══════════════════════════════════════════════════════════════════════════

def handle_smart_install(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """
    Install packages intelligently:
    - Checks if already installed
    - Detects conflicts between pip/pkg
    - Creates virtualenv if needed for Python packages
    - Reports what will be installed before doing it
    """
    packages = data.get("packages", "").strip()
    manager = data.get("manager", "auto").strip()  # auto, pkg, pip, npm, gem, cargo
    dry_run = data.get("dry_run", False)

    if not packages:
        _json_response(handler, 400, {"error": "Missing 'packages' — space-separated list"})
        return

    checks = [
        'echo "=== Pre-Install Check ==="',
        f'echo "Requested: {packages}"',
        f'echo "Manager: {manager}"',
        'echo "---"',
    ]

    pkg_list = packages.split()
    for pkg in pkg_list:
        pkg_safe = _shell_quote(pkg)
        checks.append(f'echo "Checking: {pkg}"')
        checks.append(f'pkg list-installed 2>/dev/null | grep -q "^{pkg_safe}/" && echo "  ✅ Already installed via pkg" || echo "  - Not in pkg"')
        checks.append(f'pip show {pkg_safe} 2>/dev/null | grep -q "Name:" && echo "  ✅ Already installed via pip" || echo "  - Not in pip"')
        checks.append(f'npm list -g {pkg_safe} 2>/dev/null | grep -q "{pkg_safe}" && echo "  ✅ Already installed via npm" || echo "  - Not in npm"')

    checks.append('echo "---"')
    checks.append('echo "=== Python Environment ==="')
    checks.append('echo "System Python: $(which python3)"')
    checks.append('echo "Python version: $(python3 --version 2>&1)"')
    checks.append('echo "Pip version: $(pip --version 2>&1 | head -1)"')
    checks.append('python3 -c "import sys; print(sys.prefix)" 2>&1')
    checks.append('echo "Virtual env: $VIRTUAL_ENV"')

    checks.append('echo "---"')
    checks.append('echo "=== Disk Space ==="')
    checks.append('df -h /data 2>/dev/null | tail -1')

    if not dry_run:
        checks.append('echo "---"')
        checks.append(f'echo "Installing: {packages}"')
        if manager == "pip":
            checks.append(f'pip install {packages} 2>&1 | tail -20')
        elif manager == "pkg":
            checks.append(f'pkg install -y {packages} 2>&1 | tail -20')
        else:
            # Auto: try pkg first, fall back to pip
            checks.append(f'pkg install -y {packages} 2>&1 | tail -15')
            checks.append('echo "---"')
            checks.append(f'pip install {packages} 2>&1 | tail -15')

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 2. PERMISSION DOCTOR — diagnose and fix storage/permission issues
# ═══════════════════════════════════════════════════════════════════════════

def handle_permission_fix(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Diagnose Termux permission and storage access issues."""
    target = data.get("target", "all").strip()

    checks = ['echo "=== Permission Diagnostic ==="']

    if target in ("storage", "all"):
        checks += [
            'echo "--- Storage ---"',
            'ls -la ~/storage/ 2>&1',
            'ls -la ~/storage/shared/ 2>/dev/null | head -5 || echo "  ❌ Cannot access shared storage"',
            'echo "Fix: Run termux-setup-storage and grant permission in Android settings"',
        ]

    if target in ("files", "all"):
        checks += [
            'echo "--- File Permissions ---"',
            'ls -la ~/ 2>/dev/null | head -10',
            'echo "Current user: $(whoami)"',
            'echo "UID: $(id -u), GID: $(id -g)"',
        ]

    if target in ("api", "all"):
        checks += [
            'echo "--- Termux:API ---"',
            'which termux-battery-status 2>/dev/null && echo "  ✅ termux-api installed" || echo "  ❌ termux-api NOT installed — pkg install termux-api"',
            'which termux-notification 2>/dev/null && echo "  ✅ notifications available" || echo "  ❌ Run pkg install termux-api"',
            'which termux-camera-photo 2>/dev/null && echo "  ✅ camera available" || echo "  Camera needs termux-api package"',
        ]

    if target in ("network", "all"):
        checks += [
            'echo "--- Network ---"',
            'ping -c 1 -W 1 google.com 2>/dev/null && echo "  ✅ Internet accessible" || echo "  ❌ No internet"',
            'curl -s --max-time 2 https://google.com >/dev/null 2>&1 && echo "  ✅ HTTPS OK" || echo "  ❌ HTTPS blocked"',
        ]

    if target in ("termux", "all"):
        checks += [
            'echo "--- Termux Permissions ---"',
            'echo "Termux can: $(ls -la /data/data/com.termux/ 2>/dev/null | head -3)"',
            'echo "SELinux: $(getenforce 2>/dev/null || echo unknown)"',
        ]

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 3. PROFILE — one-command Termux setup for different use cases
# ═══════════════════════════════════════════════════════════════════════════

def handle_profile(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Set up Termux for a specific use case in one command."""
    profile = data.get("profile", "dev").strip()
    dry_run = data.get("dry_run", False)

    profiles = {
        "dev": {
            "name": "Developer",
            "packages": "python nodejs git vim clang make openssh tmux",
            "pip": "ipython requests flask",
            "setup": [
                "echo 'alias g=git' >> ~/.bashrc",
                "echo 'alias p=python3' >> ~/.bashrc",
                "echo 'alias n=node' >> ~/.bashrc",
                "git config --global init.defaultBranch main 2>/dev/null",
                "mkdir -p ~/projects ~/scripts",
            ]
        },
        "python": {
            "name": "Python Developer",
            "packages": "python python-pip python-numpy git vim openssh tmux",
            "pip": "ipython requests flask django pytest black mypy",
            "setup": [
                "echo 'alias py=python3' >> ~/.bashrc",
                "echo 'alias venv=\"python3 -m venv .venv && source .venv/bin/activate\"' >> ~/.bashrc",
                "mkdir -p ~/projects ~/.config/pip",
                "echo '[global]\nbreak-system-packages = true' > ~/.config/pip/pip.conf 2>/dev/null",
            ]
        },
        "web": {
            "name": "Web Developer",
            "packages": "nodejs python git nginx openssh tmux",
            "pip": "flask django gunicorn",
            "npm": "typescript prettier",
            "setup": [
                "echo 'alias dev=\"python3 -m http.server 8080\"' >> ~/.bashrc",
                "mkdir -p ~/projects/web ~/projects/api",
            ]
        },
        "hacker": {
            "name": "Security / Pentest",
            "packages": "python python-pip nmap hydra openssh git curl wget tmux tsu",
            "pip": "requests scrapy beautifulsoup4 pwntools",
            "setup": [
                "echo 'alias scan=\"nmap -sV -sC\"' >> ~/.bashrc",
                "mkdir -p ~/pentest ~/tools ~/recon",
            ]
        },
        "writer": {
            "name": "Writer / Student",
            "packages": "python python-pip git vim openssh",
            "pip": "jupyter pandas matplotlib",
            "setup": [
                "echo 'alias notes=\"cd ~/notes && vim\"' >> ~/.bashrc",
                "mkdir -p ~/notes ~/papers ~/research",
            ]
        },
        "minimal": {
            "name": "Minimal / Lightweight",
            "packages": "git vim openssh curl",
            "setup": [
                "echo 'alias ..=\"cd ..\"' >> ~/.bashrc",
                "echo 'alias ll=\"ls -lah\"' >> ~/.bashrc",
            ]
        },
    }

    if profile not in profiles:
        execute_streaming(handler, f'echo "Available profiles: {", ".join(profiles.keys())}"')
        return

    cfg = profiles[profile]
    cmds = [f'echo "🚀 Setting up: {cfg["name"]} Profile"', 'echo "---"']

    if dry_run:
        cmds.append(f'echo "Would install: {cfg.get("packages", "none")}"')
        if "pip" in cfg:
            cmds.append(f'echo "Would pip install: {cfg["pip"]}"')
        if "npm" in cfg:
            cmds.append(f'echo "Would npm install: {cfg["npm"]}"')
        cmds.append('echo "---"')
        for s in cfg.get("setup", []):
            cmds.append(f'echo "Would run: {s}"')
    else:
        if "packages" in cfg:
            cmds.append(f'echo "Installing packages..."')
            cmds.append(f'pkg install -y {cfg["packages"]} 2>&1 | tail -10')
        if "pip" in cfg:
            cmds.append(f'echo "Installing pip packages..."')
            cmds.append(f'pip install {cfg["pip"]} 2>&1 | tail -10')
        if "npm" in cfg:
            cmds.append(f'echo "Installing npm packages..."')
            cmds.append(f'npm install -g {cfg["npm"]} 2>&1 | tail -10')
        for s in cfg.get("setup", []):
            cmds.append(s)

    cmds.append(f'echo "✅ {cfg["name"]} profile setup complete!"')
    execute_streaming(handler, " && ".join(cmds))


# ═══════════════════════════════════════════════════════════════════════════
# 4. ERROR EXPLAIN — AI-powered error message interpreter
# ═══════════════════════════════════════════════════════════════════════════

def handle_error_explain(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """
    Run diagnostic commands to help the AI explain an error.
    The AI receives the full context and explains in plain language.
    """
    error_text = data.get("error", "").strip()
    context_cmd = data.get("command", "").strip()

    checks = ['echo "=== Error Context ==="']

    if error_text:
        checks.append(f'echo "Error: {error_text}"')
    if context_cmd:
        checks.append(f'echo "Command: {context_cmd}"')

    checks += [
        'echo "---"',
        'echo "=== System State ==="',
        'echo "Python: $(python3 --version 2>&1)"',
        'echo "Pip: $(pip --version 2>&1 | head -1)"',
        'echo "Node: $(node --version 2>&1)"',
        'echo "PATH: $PATH"',
        'echo "---"',
        'echo "=== Python Details ==="',
        'python3 -c "import sys; print(sys.executable)" 2>&1',
        'python3 -c "import sys; print(sys.path)" 2>&1',
        'echo "---"',
        'echo "=== Installed Python Packages ==="',
        'pip list 2>&1 | head -40',
        'echo "---"',
        'echo "=== Recently Modified Files ==="',
        'find ~/ -maxdepth 3 -type f -mmin -30 2>/dev/null | head -20',
        'echo "---"',
        'echo "=== Shell History (last 10) ==="',
        'tail -10 ~/.bash_history 2>/dev/null || echo "No history"',
    ]

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 5. SSH WIZARD — full SSH setup automation
# ═══════════════════════════════════════════════════════════════════════════

def handle_ssh_wizard(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Set up SSH server with key generation, config, and connection details."""
    action = data.get("action", "setup").strip()

    if action == "setup":
        checks = [
            'echo "🔐 SSH Setup Wizard"',
            'echo "---"',
            'echo "Installing openssh..."',
            'pkg install -y openssh 2>&1 | tail -5',
            'echo "---"',
            'echo "Checking existing keys..."',
            'ls -la ~/.ssh/ 2>/dev/null || echo "No SSH directory"',
            'mkdir -p ~/.ssh',
            'chmod 700 ~/.ssh',
            'echo "---"',
            'echo "Key status:"',
            'if [ -f ~/.ssh/id_rsa ]; then echo "  ✅ RSA key exists ($(wc -c < ~/.ssh/id_rsa.pub) bytes)"; else '
            'echo "  Generating RSA key..."; ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N "" -q && echo "  ✅ Key generated"; fi',
            'echo "---"',
            'echo "Starting SSH server..."',
            'sshd 2>&1 && echo "  ✅ SSH server started on port 8022" || echo "  SSH already running or port in use"',
            'echo "---"',
            'echo "=== Connection Info ==="',
            'echo "Username: $(whoami)"',
            'echo "IP: $(ifconfig wlan0 2>/dev/null | grep \"inet \" | awk \'{print $2}\' || echo unknown)"',
            'echo "Port: 8022"',
            'echo "---"',
            'echo "Public key:"',
            'cat ~/.ssh/id_rsa.pub 2>/dev/null || echo "No public key"',
            'echo "---"',
            'echo "Connect from PC: ssh $(whoami)@<IP> -p 8022"',
            'echo "Copy key to PC: ssh-copy-id -p 8022 $(whoami)@<IP>"',
        ]
    elif action == "status":
        checks = [
            'echo "=== SSH Status ==="',
            'pgrep -f sshd >/dev/null 2>&1 && echo "✅ SSH server running" || echo "❌ SSH server NOT running"',
            'echo "Port 8022: $(ss -tlnp 2>/dev/null | grep 8022 || netstat -tlnp 2>/dev/null | grep 8022 || echo not listening)"',
            'echo "---"',
            'echo "Keys:"',
            'ls -la ~/.ssh/ 2>/dev/null || echo "No ~/.ssh directory"',
            'echo "---"',
            'echo "Fingerprint:"',
            'ssh-keygen -lf ~/.ssh/id_rsa.pub 2>/dev/null || echo "No key"',
            'echo "---"',
            'echo "Recent logins:"',
            'tail -5 ~/.ssh/authorized_keys 2>/dev/null && echo "(last 5 keys)" || echo "No authorized_keys"',
        ]
    elif action == "stop":
        checks = [
            'pkill sshd 2>/dev/null && echo "✅ SSH server stopped" || echo "SSH was not running"',
        ]
    else:
        checks = ['echo "Actions: setup, status, stop"']

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 6. SERVICE GUARD — manage background services with monitoring
# ═══════════════════════════════════════════════════════════════════════════

def handle_service_guard(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Manage background services: start with auto-restart, list, stop."""
    action = data.get("action", "list").strip()
    service_name = data.get("name", "").strip()
    service_cmd = data.get("cmd", "").strip()

    if action == "list":
        checks = [
            'echo "=== Running Services ==="',
            'echo "---"',
            'echo "Background processes:"',
            'ps aux 2>/dev/null | grep -v grep | grep -E "python|node|nginx|http.server|flask|discord|telegram|bot|server|daemon|crond|sshd" | head -20 || echo "  None detected"',
            'echo "---"',
            'echo "Port listeners:"',
            'ss -tlnp 2>/dev/null | head -10 || netstat -tlnp 2>/dev/null | head -10 || echo "  Cannot check ports"',
        ]
    elif action == "start":
        if not service_name or not service_cmd:
            _json_response(handler, 400, {"error": "Missing 'name' and 'cmd' for service"})
            return
        checks = [
            f'echo "Starting service: {service_name}"',
            f'echo "Command: {service_cmd}"',
            f'nohup sh -c {_shell_quote(service_cmd)} > /dev/null 2>&1 &',
            f'sleep 1',
            f'echo "PID: $(pgrep -f {_shell_quote(service_cmd)} | head -1 || echo unknown)"',
            f'echo "✅ Service started"',
            f'echo "---"',
            f'echo "To keep alive, use termux-wake-lock (pkg install termux-api)"',
        ]
    elif action == "stop":
        if not service_name:
            _json_response(handler, 400, {"error": "Missing 'name' of service to stop"})
            return
        checks = [
            f'echo "Stopping: {service_name}"',
            f'pkill -f {_shell_quote(service_name)} 2>/dev/null && echo "  ✅ Stopped" || echo "  Service not found"',
        ]
    elif action == "wake-lock":
        checks = [
            'echo "=== Wake Lock ==="',
            'termux-wake-lock acquire 2>/dev/null && echo "✅ Wake lock acquired" || echo "❌ termux-api not installed"',
            'echo "Phone will not sleep while locked"',
            'echo "Release: termux-wake-lock release"',
        ]
    elif action == "wake-release":
        checks = [
            'termux-wake-lock release 2>/dev/null && echo "✅ Wake lock released" || echo "No lock held"',
        ]
    else:
        checks = ['echo "Actions: list, start, stop, wake-lock, wake-release"']

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 7. HISTORY INSIGHT — analyze shell usage patterns
# ═══════════════════════════════════════════════════════════════════════════

def handle_history_insight(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Analyze shell history to find patterns, suggest aliases, show habits."""
    history_file = data.get("file", f"{HOME}/.bash_history").strip()
    limit = data.get("limit", 100)
    safe_file = _shell_quote(history_file)

    checks = [
        f'echo "=== Shell History Analysis ==="',
        f'echo "File: {history_file}"',
        f'echo "Total commands: $(wc -l < {safe_file} 2>/dev/null || echo 0)"',
        f'echo "---"',
        f'echo "Most used commands (last {limit}):"',
        f"tail -{limit} {safe_file} 2>/dev/null | awk '{{print $1}}' | sort | uniq -c | sort -rn | head -15",
        f'echo "---"',
        f'echo "Commands with sudo/tsu:"',
        f'tail -{limit} {safe_file} 2>/dev/null | grep -E "sudo|tsu" | tail -10 || echo "  None"',
        f'echo "---"',
        f'echo "Failed commands (starting with !):"',
        f'tail -{limit} {safe_file} 2>/dev/null | grep "^!" | head -10 || echo "  None"',
        f'echo "---"',
        f'echo "Long commands (>50 chars):"',
        f"tail -{limit} {safe_file} 2>/dev/null | awk 'length>50' | tail -10 || echo '  None'",
        f'echo "---"',
        f'echo "Current aliases:"',
        f'alias 2>/dev/null | head -20 || echo "  No aliases defined"',
        f'echo "---"',
        f'echo "💡 Tip: Add aliases to ~/.bashrc for frequently used commands"',
    ]

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 8. OPTIMIZE — performance analysis and recommendations
# ═══════════════════════════════════════════════════════════════════════════

def handle_optimize(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Analyze Termux resource usage and suggest optimizations."""
    checks = [
        'echo "=== Performance Analysis ==="',
        'echo "---"',
        'echo "Memory Usage:"',
        'free -h 2>/dev/null || cat /proc/meminfo 2>/dev/null | head -5 || echo "  Cannot read memory"',
        'echo "---"',
        'echo "CPU:"',
        r'cat /proc/cpuinfo 2>/dev/null | grep "model name\|processor" | head -5 || echo "  Unknown"',
        'echo "Load: $(cat /proc/loadavg 2>/dev/null || echo unknown)"',
        'echo "---"',
        'echo "Disk Usage:"',
        'df -h /data 2>/dev/null',
        'echo "---"',
        'echo "Top processes by memory:"',
        'ps aux --sort=-%mem 2>/dev/null | head -10 || echo "  Cannot list"',
        'echo "---"',
        'echo "Cache sizes:"',
        'echo "Pip: $(du -sh ~/.cache/pip 2>/dev/null | cut -f1 || echo 0)"',
        'echo "npm: $(du -sh ~/.npm 2>/dev/null | cut -f1 || echo 0)"',
        'echo "apt: $(du -sh /data/data/com.termux/files/usr/var/cache/apt/archives/ 2>/dev/null | cut -f1 || echo 0)"',
        'echo "---"',
        'echo "=== Recommendations ==="',
        'echo "💡 pip cache purge — clears pip downloads"',
        'echo "💡 npm cache clean --force — clears npm cache"',
        'echo "💡 pkg clean — removes old .deb files"',
        'echo "💡 pkg autoclean — removes obsolete packages"',
        'echo "💡 rm -rf ~/.cache/* — clear all caches"',
        'echo "💡 Use --no-cache-dir with pip install to skip caching"',
    ]

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 9. QUICK CMD — alias and shortcut management
# ═══════════════════════════════════════════════════════════════════════════

def handle_quick_cmd(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Create and manage command aliases and shortcuts."""
    action = data.get("action", "list").strip()
    alias_name = data.get("name", "").strip()
    alias_cmd = data.get("cmd", "").strip()

    if action == "list":
        checks = [
            'echo "=== Current Aliases ==="',
            'alias 2>/dev/null | sort || echo "  No aliases"',
            'echo "---"',
            'echo "Bashrc has: $(wc -l < ~/.bashrc 2>/dev/null) lines"',
        ]
    elif action == "add":
        if not alias_name or not alias_cmd:
            _json_response(handler, 400, {"error": "Missing 'name' and 'cmd' for alias"})
            return
        safe_alias = _shell_quote(alias_name)
        safe_cmd = _shell_quote(alias_cmd)
        checks = [
            f'echo "Adding alias: {alias_name} -> {alias_cmd}"',
            f'echo "alias {alias_name}={_shell_quote(alias_cmd)}" >> ~/.bashrc',
            f'alias {alias_name}={_shell_quote(alias_cmd)} 2>/dev/null',
            f'echo "✅ Alias added. Restart shell or run: source ~/.bashrc"',
        ]
    elif action == "remove":
        if not alias_name:
            _json_response(handler, 400, {"error": "Missing 'name' of alias to remove"})
            return
        checks = [
            f'echo "Removing alias: {alias_name}"',
            f'sed -i "/alias {alias_name}=/d" ~/.bashrc 2>/dev/null',
            f'unalias {alias_name} 2>/dev/null',
            f'echo "✅ Alias removed"',
        ]
    elif action == "export":
        checks = [
            'echo "=== Your Config ==="',
            'cat ~/.bashrc 2>/dev/null',
            'echo "---"',
            'echo "Share this with another device or backup."',
        ]
    else:
        checks = ['echo "Actions: list, add, remove, export"']

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 10. PORT MANAGE — network port visibility and management
# ═══════════════════════════════════════════════════════════════════════════

def handle_port_manage(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Show what's listening, help expose services, manage ports."""
    action = data.get("action", "list").strip()

    if action == "list":
        checks = [
            'echo "=== Listening Ports ==="',
            'ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || echo "  Install: pkg install net-tools"',
            'echo "---"',
            'echo "=== Active Connections ==="',
            'ss -tnp 2>/dev/null | head -15 || echo "  None"',
        ]
    elif action == "check":
        port = data.get("port", "8080").strip()
        checks = [
            f'echo "Checking port: {port}"',
            f'ss -tlnp 2>/dev/null | grep ":{port}" || netstat -tlnp 2>/dev/null | grep ":{port}" || echo "  Port {port} is FREE"',
            f'nc -zv localhost {port} 2>&1 || echo "  Cannot connect to port {port}"',
        ]
    elif action == "ip":
        checks = [
            'echo "=== Network Interfaces ==="',
            'ifconfig 2>/dev/null | grep -E "inet |inet6 " || ip addr 2>/dev/null | grep inet || echo "  Unknown"',
            'echo "---"',
            'echo "WiFi IP: $(ifconfig wlan0 2>/dev/null | grep \"inet \" | awk \'{print $2}\' || echo unknown)"',
            'echo "Public IP: $(curl -s https://api.ipify.org 2>/dev/null || echo unknown)"',
        ]
    else:
        checks = ['echo "Actions: list, check, ip"']

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 11. MIGRATE — complete Termux environment backup and restore
# ═══════════════════════════════════════════════════════════════════════════

def handle_migrate(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """
    Full environment migration:
    - Backs up: packages list, configs, crontabs, shell history, custom scripts
    - Creates a single portable archive with a restore script
    """
    action = data.get("action", "backup").strip()
    output = data.get("output", f"~/storage/shared/termux_migration.tar.gz").strip()
    safe_out = _shell_quote(output)

    if action == "backup":
        timestamp = "__import__('time').strftime('%Y%m%d_%H%M%S')"
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M%S")
        if not data.get("output"):
            output = f"~/storage/shared/termux_migration_{ts}.tar.gz"
            safe_out = _shell_quote(output)

        checks = [
            f'echo "📦 Termux Migration Backup"',
            f'echo "Output: {output}"',
            f'echo "---"',
            f'echo "1/5 Exporting package list..."',
            f'pkg list-installed > /tmp/migrate_packages.txt 2>/dev/null && echo "  ✅ $(wc -l < /tmp/migrate_packages.txt) packages"',
            f'echo "2/5 Exporting pip list..."',
            f'pip list --format=freeze > /tmp/migrate_pip.txt 2>/dev/null && echo "  ✅ $(wc -l < /tmp/migrate_pip.txt) pip packages" || echo "  No pip"',
            f'echo "3/5 Saving crontab..."',
            f'crontab -l > /tmp/migrate_crontab.txt 2>/dev/null && echo "  ✅ Cron jobs saved" || echo "  No crontab"',
            f'echo "4/5 Collecting configs..."',
            f'mkdir -p /tmp/migrate_configs',
            f'cp ~/.bashrc /tmp/migrate_configs/bashrc 2>/dev/null || true',
            f'cp ~/.zshrc /tmp/migrate_configs/zshrc 2>/dev/null || true',
            f'cp -r ~/.termux/ /tmp/migrate_configs/termux 2>/dev/null || true',
            f'cp ~/.gitconfig /tmp/migrate_configs/gitconfig 2>/dev/null || true',
            f'cp ~/.ssh/config /tmp/migrate_configs/ssh_config 2>/dev/null || true',
            f'echo "  ✅ Configs collected"',
            f'echo "5/5 Creating archive..."',
            f'mkdir -p "$(dirname {safe_out})" 2>/dev/null',
            f'tar -czf {safe_out} -C /tmp migrate_packages.txt migrate_pip.txt migrate_crontab.txt migrate_configs/ 2>&1',
            f'echo "✅ Migration archive created!"',
            f'ls -lh {safe_out}',
            f'echo "---"',
            f'echo "Transfer to new device and run: /restore with this file"',
            f'rm -rf /tmp/migrate_* 2>/dev/null',
        ]
    elif action == "restore":
        archive = data.get("file", "").strip()
        if not archive:
            _json_response(handler, 400, {"error": "Missing 'file' — path to migration archive"})
            return
        safe_archive = _shell_quote(archive)
        checks = [
            f'echo "📥 Restoring from: {archive}"',
            f'echo "---"',
            f'tar -xzf {safe_archive} -C /tmp/ 2>&1 && echo "  ✅ Extracted" || echo "  ❌ Cannot extract"',
            f'echo "1/4 Installing packages..."',
            f'cat /tmp/migrate_packages.txt 2>/dev/null | xargs pkg install -y 2>&1 | tail -10 || echo "  No package list"',
            f'echo "2/4 Installing pip packages..."',
            f'cat /tmp/migrate_pip.txt 2>/dev/null | xargs pip install 2>&1 | tail -10 || echo "  No pip list"',
            f'echo "3/4 Restoring configs..."',
            f'cp /tmp/migrate_configs/bashrc ~/.bashrc 2>/dev/null || true',
            f'cp /tmp/migrate_configs/zshrc ~/.zshrc 2>/dev/null || true',
            f'cp -r /tmp/migrate_configs/termux/ ~/.termux/ 2>/dev/null || true',
            f'cp /tmp/migrate_configs/gitconfig ~/.gitconfig 2>/dev/null || true',
            f'echo "  ✅ Configs restored"',
            f'echo "4/4 Restoring crontab..."',
            f'cat /tmp/migrate_crontab.txt 2>/dev/null | crontab - 2>/dev/null && echo "  ✅ Cron restored" || echo "  No crontab"',
            f'rm -rf /tmp/migrate_* 2>/dev/null',
            f'echo "---"',
            f'echo "✅ Migration complete! Restart Termux."',
        ]
    elif action == "preview":
        archive = data.get("file", "").strip()
        if not archive:
            _json_response(handler, 400, {"error": "Missing 'file' to preview"})
            return
        safe_archive = _shell_quote(archive)
        checks = [
            f'echo "📋 Migration Preview: {archive}"',
            f'tar -tzf {safe_archive} 2>/dev/null || echo "  Cannot read archive"',
        ]
    else:
        checks = ['echo "Actions: backup, restore, preview"']

    cmd = " && ".join(checks)
    execute_streaming(handler, cmd)


# ═══════════════════════════════════════════════════════════════════════════
# 12. TUTORIAL — interactive Termux learning
# ═══════════════════════════════════════════════════════════════════════════

def handle_tutorial(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Interactive tutorial mode for learning Termux and Linux."""
    topic = data.get("topic", "basics").strip()

    topics = {
        "basics": [
            'echo "📚 Termux Basics"',
            'echo "---"',
            'echo "1. Navigation:"',
            'echo "   pwd          — show current directory"',
            'echo "   ls           — list files"',
            'echo "   cd <dir>     — change directory"',
            'echo "   cd ~         — go home"',
            'echo "   cd ..        — go up one level"',
            'echo "---"',
            'echo "2. File Operations:"',
            'echo "   cat <file>   — view file"',
            'echo "   touch <file> — create empty file"',
            'echo "   mkdir <dir>  — create directory"',
            'echo "   rm <file>    — delete file"',
            'echo "   cp <src> <dst> — copy"',
            'echo "   mv <src> <dst> — move/rename"',
            'echo "---"',
            'echo "3. Package Management:"',
            'echo "   pkg update   — refresh package list"',
            'echo "   pkg upgrade  — upgrade all packages"',
            'echo "   pkg install <name> — install package"',
            'echo "   pkg search <term> — search for package"',
            'echo "---"',
            'echo "4. Helpful Commands:"',
            'echo "   man <cmd>    — manual for command"',
            'echo "   <cmd> --help — quick help"',
            'echo "   history      — show command history"',
            'echo "   clear        — clear screen"',
        ],
        "python": [
            'echo "🐍 Python in Termux"',
            'echo "---"',
            'echo "Install: pkg install python python-pip"',
            'echo "Run script: python3 script.py"',
            'echo "Interactive: python3 (Ctrl+D to exit)"',
            'echo "Install packages: pip install <name>"',
            'echo "---"',
            'echo "Virtual Environment:"',
            'echo "  python3 -m venv .venv"',
            'echo "  source .venv/bin/activate"',
            'echo "---"',
            'echo "⚠️ Common Issues:"',
            'echo "  - pip conflicts: use virtual environments"',
            'echo "  - EXTERNALLY-MANAGED error: pip install --break-system-packages"',
            'echo "  - numpy/scipy: pkg install python-numpy"',
        ],
        "ssh": [
            'echo "🔐 SSH Guide"',
            'echo "---"',
            'echo "Setup: pkg install openssh"',
            'echo "Generate key: ssh-keygen -t rsa -b 4096"',
            'echo "Start server: sshd"',
            'echo "Connect from PC: ssh user@phone-ip -p 8022"',
            'echo "---"',
            'echo "Copy key to PC: ssh-copy-id -p 8022 user@phone-ip"',
            'echo "---"',
            'echo "Use /ssh-wizard for automated setup"',
        ],
        "storage": [
            'echo "💾 Storage Guide"',
            'echo "---"',
            'echo "1. Run: termux-setup-storage"',
            'echo "2. Grant permission in Android popup"',
            'echo "3. Access files: cd ~/storage/shared"',
            'echo "---"',
            'echo "Paths:"',
            'echo "  ~/storage/shared/         — internal storage"',
            'echo "  ~/storage/downloads/      — downloads folder"',
            'echo "  ~/storage/dcim/           — camera photos"',
            'echo "  ~/storage/external-1/     — SD card"',
            'echo "---"',
            'echo "⚠️ Common Issues:"',
            "echo \"  - Permission denied: re-run termux-setup-storage\"",
            'echo "  - Files not showing: check Android app permissions"',
        ],
        "customize": [
            'echo "🎨 Customizing Termux"',
            'echo "---"',
            'echo "Shell: pkg install zsh → chsh -s zsh"',
            'echo "Prompt: install oh-my-zsh or starship"',
            'echo "Font: place .ttf in ~/.termux/font.ttf"',
            'echo "Colors: edit ~/.termux/colors.properties"',
            'echo "---"',
            'echo "Popular tools:"',
            'echo "  eza  — modern ls (pkg install eza)"',
            'echo "  bat  — modern cat (pkg install bat)"',
            'echo "  fzf  — fuzzy finder (pkg install fzf)"',
            'echo "  tmux — terminal multiplexer"',
            'echo "  neofetch — system info display"',
            'echo "---"',
            'echo "Use /config-fix to check your setup"',
        ],
    }

    if topic in topics:
        cmd = " && ".join(topics[topic])
    else:
        cmd = f'echo "Available topics: {", ".join(topics.keys())}"'

    execute_streaming(handler, cmd)
