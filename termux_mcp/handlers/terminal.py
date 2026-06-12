"""
Termux Power-Tool Handlers — AI-native features built on Termux's Linux environment.
Diagnostics, package intelligence, dev environments, code review, backups, and more.
"""
import os
from typing import TYPE_CHECKING

from ..shell import execute_streaming, get_current_dir

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


def _json_response(handler: "BaseHTTPRequestHandler", status: int, payload: dict) -> None:
    import json
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _shell_quote(s: str) -> str:
    """Wrap in single quotes, properly escaping embedded single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


# ── AI Auto-Fixer /diagnose ──────────────────────────────────────────────

def handle_diagnose(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Run diagnostic commands and report what's broken."""
    intent = data.get("intent", "python").strip()
    checks = []

    if intent in ("python", "pip", "all"):
        checks += [
            'echo "=== Python ==="',
            'python3 --version 2>&1 || echo "Missing: python3"',
            'pip --version 2>&1 || echo "Missing: pip"',
            'python3 -c "import sys; print(sys.executable)" 2>&1 || echo "Cant import sys"',
            'echo "=== PATH ==="',
            'echo "$PATH"',
        ]
    if intent in ("pip", "all"):
        checks += [
            'echo "=== Pip Packages ==="',
            'pip list 2>&1 | head -30 || echo "Pip broken"',
            'pip config list 2>&1 || echo "No pip config"',
        ]
    if intent in ("node", "npm", "all"):
        checks += [
            'echo "=== Node.js ==="',
            'node --version 2>&1 || echo "Missing: nodejs"',
            'npm --version 2>&1 || echo "Missing: npm"',
        ]
    if intent in ("git", "all"):
        checks += [
            'echo "=== Git ==="',
            'git --version 2>&1 || echo "Missing: git"',
        ]
    if intent in ("storage", "all"):
        checks += [
            'echo "=== Storage ==="',
            'ls -la ~/storage/ 2>&1 || echo "Storage not setup - run termux-setup-storage"',
            'df -h /data 2>/dev/null || echo "Cannot check disk"',
        ]
    if intent in ("packages", "all"):
        checks += [
            'echo "=== Packages ==="',
            'pkg list-installed 2>&1 | wc -l || echo "Cant list packages"',
            'apt list --upgradable 2>&1 | head -10 || echo "Cant check updates"',
        ]

    cmd = "\n".join(checks) if checks else 'echo "No diagnostic target specified"'
    execute_streaming(handler, cmd)


# ── Intelligent Package Install /pkg-smart ───────────────────────────────

def handle_pkg_smart(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Intent-based package discovery and install."""
    intent = data.get("intent", "").strip().lower()
    if not intent:
        _json_response(handler, 400, {"error": "Missing 'intent'. Describe what you want to do."})
        return

    do_install = data.get("install", False)
    # Map intents to packages and a post-install command
    import re

    intent_map = {
        "video":      ("ffmpeg imagemagick", "ffmpeg -version"),
        "edit video": ("ffmpeg imagemagick", "ffmpeg -version"),
        "image":      ("imagemagick", "convert --version"),
        "edit image": ("imagemagick", "convert --version"),
        "photo":      ("imagemagick", "convert --version"),
        "web server": ("python nginx", "python3 --version"),
        "python web": ("python python-pip nginx", "python3 --version"),
        "python":     ("python python-pip", "python3 --version"),
        "node":       ("nodejs", "node --version"),
        "nodejs":     ("nodejs", "node --version"),
        "javascript": ("nodejs", "node --version"),
        "react":      ("nodejs", "node --version"),
        "c":          ("clang make gdb", "clang --version"),
        "c++":        ("clang make gdb", "clang++ --version"),
        "cpp":        ("clang make gdb", "clang++ --version"),
        "rust":       ("rust binutils", "rustc --version"),
        "go":         ("golang", "go version"),
        "java":       ("openjdk-17", "java --version"),
        "ruby":       ("ruby", "ruby --version"),
        "php":        ("php", "php --version"),
        "git":        ("git", "git --version"),
        "database":   ("sqlite mariadb", "sqlite3 --version"),
        "sqlite":     ("sqlite", "sqlite3 --version"),
        "ssh":        ("openssh", "ssh -V 2>&1"),
        "ftp":        ("openssh curl", "curl --version"),
        "editor":     ("vim neovim nano", "vim --version 2>&1 | head -1"),
        "terminal":   ("zsh termux-api fzf bat eza", "zsh --version"),
        "beautiful":  ("zsh eza bat neofetch", "neofetch --version 2>&1"),
        "customize":  ("zsh eza bat neofetch", "neofetch --version 2>&1"),
        "irc":        ("irssi", "irssi --version 2>&1"),
        "bot":        ("python python-pip", "python3 --version"),
        "discord bot":("python python-pip", "python3 --version"),
        "telegram bot":("python python-pip", "python3 --version"),
        "scrape":     ("python python-pip curl", "python3 --version"),
        "scraping":   ("python python-pip curl", "python3 --version"),
        "ocr":        ("tesseract imagemagick", "tesseract --version 2>&1"),
        "text":       ("tesseract imagemagick", "tesseract --version 2>&1"),
        "qr":         ("qrencode zbar", "qrencode --version 2>&1"),
        "barcode":    ("qrencode zbar", "qrencode --version 2>&1"),
        "tor":        ("tor torsocks", "tor --version 2>&1"),
        "speed":      ("speedtest-cli", "speedtest-cli --version 2>&1"),
        "speedtest":  ("speedtest-cli", "speedtest-cli --version 2>&1"),
        "translate":  ("python python-pip", "python3 --version"),
        "science":    ("python python-pip python-numpy", "python3 --version"),
        "data":       ("python python-pip python-numpy python-pandas", "python3 --version"),
        "ml":         ("python python-pip python-numpy", "python3 --version"),
        "ai":         ("python python-pip", "python3 --version"),
        "network":    ("nmap curl wget openssh", "nmap --version 2>&1 | head -1"),
        "hack":       ("nmap python python-pip openssh hydra", "nmap --version 2>&1 | head -1"),
        "pentest":    ("nmap python python-pip openssh hydra", "nmap --version 2>&1 | head -1"),
        "archive":    ("zip unzip tar", "tar --version 2>&1 | head -1"),
        "compress":   ("zip unzip tar", "tar --version 2>&1 | head -1"),
    }

    best_match = None
    best_score = 0
    for key, (pkgs, verify) in intent_map.items():
        # Simple fuzzy matching
        score = 0
        for word in intent.split():
            if word in key:
                score += len(word)
        if key == intent:
            score = 999
        elif intent in key or key in intent:
            score = max(score, 900)
        if score > best_score:
            best_score = score
            best_match = (key, pkgs, verify)

    if best_match and best_score > 0:
        key, pkgs, verify_cmd = best_match
        if do_install:
            cmd = f'echo "Installing: {pkgs}"; pkg install -y {pkgs} 2>&1 | tail -20 && echo "---" && {verify_cmd} && echo "Done! All packages installed." || echo "Some packages may have failed."'
        else:
            cmd = f'echo "📦 Suggested packages for: {key}"; echo "Command: pkg install {pkgs}"; echo "---"; echo "Already installed:"; for pkg in {pkgs.split()}; do dpkg -s "$pkg" 2>/dev/null | grep Status | head -1 || echo "  $pkg - NOT installed"; done; echo "---"; echo "To install, use: install: true"'
    else:
        cmd = f'echo "No matching packages found for: {intent}"; echo "Try broader terms like: python, video, web server, nodejs, c++"'

    execute_streaming(handler, cmd)


# ── Command Explainer /explain ───────────────────────────────────────────

def handle_explain(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Explain what a shell command does before running it."""
    cmd = data.get("cmd", "").strip()
    if not cmd:
        _json_response(handler, 400, {"error": "Missing 'cmd' to explain"})
        return

    # Build a comprehensive explanation using the shell
    parts = []
    if "|" in cmd:
        parts.append(f'echo "🔗 Pipeline: {cmd}"')
    if ">" in cmd or ">>" in cmd:
        parts.append(f'echo "📝 Redirects output to a file"')
    if "&&" in cmd:
        parts.append(f'echo "⛓️ Chained commands — second runs only if first succeeds"')
    if "||" in cmd:
        parts.append(f'echo "⛓️ Fallback — right side runs if left side fails"')
    if "rm -rf" in cmd or "rm -r" in cmd:
        parts.append(f'echo "⚠️ DANGER: rm -r will DELETE files/directories permanently"')
    if "rm " in cmd and "-rf" not in cmd:
        parts.append(f'echo "🗑️ This will DELETE files — make sure you have backups"')
    if cmd.startswith("cd "):
        parts.append(f'echo "📂 Changes directory"')
    if "sudo" in cmd:
        parts.append(f'echo "⚠️ sudo requires root — Termux does not support sudo by default"')
    if "apt " in cmd or "pkg " in cmd:
        parts.append(f'echo "📦 Package manager operation"')
    if "git " in cmd:
        parts.append(f'echo "🔄 Git version control operation"')
    if "pip " in cmd:
        parts.append(f'echo "🐍 Python package manager (pip)"')
    if "npm " in cmd:
        parts.append(f'echo "📦 Node.js package manager (npm)"')

    # Try which to resolve the command
    first_word = cmd.split()[0] if cmd.split() else ""
    if first_word:
        parts.append(f'echo "---"')
        parts.append(f'which {_shell_quote(first_word)} 2>/dev/null && echo "✅ Command found" || echo "❌ Command NOT found — you may need to install it"')

    script = "\n".join(parts) if parts else f'echo "Command: {cmd}"'
    execute_streaming(handler, f'{script}\necho "---"\necho "Original command: {cmd}"')


# ── Dev Environment Setup /dev-env ───────────────────────────────────────

def handle_dev_env(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """One-click development environment setup."""
    intent = data.get("intent", "python").strip().lower()
    cwd = get_current_dir()
    project_name = data.get("name", "myproject").strip()

    setups = {
        "python": (
            "Python Web Development",
            f'pkg install -y python python-pip 2>&1 | tail -5 && '
            f'pip install flask gunicorn 2>&1 | tail -5 && '
            f'mkdir -p {_shell_quote(cwd + "/" + project_name)} && '
            f'cd {_shell_quote(cwd + "/" + project_name)} && '
            f'echo "from flask import Flask\\napp = Flask(__name__)\\n@app.route(\'/\')\\ndef hello():\\n    return {{\'status\': \'ok\'}}\\nif __name__ == \'__main__\':\\n    app.run(host=\'0.0.0.0\', port=5000)" > app.py && '
            f'echo "Created: {project_name}/app.py" && '
            f'echo "Run: cd {project_name} && python app.py"'
        ),
        "bot": (
            "Telegram/Discord Bot",
            f'pkg install -y python python-pip 2>&1 | tail -3 && '
            f'pip install python-telegram-bot discord.py 2>&1 | tail -3 && '
            f'mkdir -p {_shell_quote(cwd + "/" + project_name)} && '
            f'cd {_shell_quote(cwd + "/" + project_name)} && '
            f'echo "# Bot token (get from @BotFather or Discord Developer Portal)\\nTOKEN = \'your_token_here\'\\nprint(\'Bot ready!\')" > bot.py && '
            f'echo "Created: {project_name}/bot.py"'
        ),
        "react": (
            "React Frontend",
            f'pkg install -y nodejs 2>&1 | tail -3 && '
            f'npm --version 2>&1 && '
            f'cd {_shell_quote(cwd)} && '
            f'echo "Run: npx create-react-app {project_name}"'
        ),
        "node": (
            "Node.js Backend",
            f'pkg install -y nodejs 2>&1 | tail -3 && '
            f'mkdir -p {_shell_quote(cwd + "/" + project_name)} && '
            f'cd {_shell_quote(cwd + "/" + project_name)} && '
            f'npm init -y 2>&1 | tail -3 && '
            f'echo "console.log(\'Server ready\');" > index.js && '
            f'echo "Created: {project_name}/index.js"'
        ),
        "c": (
            "C/C++ Development",
            f'pkg install -y clang make gdb 2>&1 | tail -3 && '
            f'mkdir -p {_shell_quote(cwd + "/" + project_name)} && '
            f'cd {_shell_quote(cwd + "/" + project_name)} && '
            f'echo "#include <stdio.h>\\nint main() {{\\n    printf(\\"Hello from Termux!\\\\n\\");\\n    return 0;\\n}}" > main.c && '
            f'echo "Created: {project_name}/main.c" && '
            f'echo "Compile: cd {project_name} && clang main.c -o main && ./main"'
        ),
        "rust": (
            "Rust Development",
            f'pkg install -y rust binutils 2>&1 | tail -3 && '
            f'cd {_shell_quote(cwd)} && '
            f'cargo new {_shell_quote(project_name)} 2>&1 && '
            f'echo "Created: {project_name}/" && '
            f'echo "Run: cd {project_name} && cargo run"'
        ),
        "data": (
            "Data Science",
            f'pkg install -y python python-pip 2>&1 | tail -3 && '
            f'pip install numpy pandas matplotlib jupyter 2>&1 | tail -5 && '
            f'mkdir -p {_shell_quote(cwd + "/" + project_name)} && '
            f'echo "Created: {project_name}/" && '
            f'echo "Start Jupyter: jupyter notebook"'
        ),
        "termux": (
            "Beautiful Terminal",
            f'pkg install -y zsh eza bat neofetch git 2>&1 | tail -5 && '
            f'echo "Installing oh-my-zsh..." && '
            f'sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended 2>&1 | tail -5 && '
            f'echo "Done! Restart Termux or run: zsh"'
        ),
    }

    if intent in setups:
        title, cmd = setups[intent]
        execute_streaming(handler, f'echo "🚀 Setting up: {title}"; echo "---"; {cmd}')
    else:
        execute_streaming(handler, f'echo "Available environments:"; echo "python, bot, react, node, c, rust, data, termux"')


# ── Code Reviewer /review ────────────────────────────────────────────────

def handle_review(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Basic static analysis — the AI will do deeper review from the output."""
    file_path = data.get("file", "").strip()
    if not file_path:
        _json_response(handler, 400, {"error": "Missing 'file' path"})
        return

    safe_path = _shell_quote(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    checks = [
        f'echo "=== File: {file_path} ==="',
        f'echo "Size: $(wc -c < {safe_path} 2>/dev/null) bytes, Lines: $(wc -l < {safe_path} 2>/dev/null)"',
        f'echo "---"',
        f'echo "Content:"',
        f'cat {safe_path} 2>/dev/null || echo "Cannot read file"',
    ]

    if ext == ".py":
        checks += [
            f'echo "---"',
            f'echo "=== Python Syntax Check ==="',
            f'python3 -m py_compile {safe_path} 2>&1 && echo "✅ Syntax OK" || echo "❌ Syntax Error"',
            f'echo "---"',
            f'echo "=== Unused Variables ==="',
            f"python3 -m flake8 {safe_path} --select F841 2>&1 | head -10 || echo 'flake8 not installed (pip install flake8)'",
        ]
    elif ext in (".sh", ".bash"):
        checks += [
            f'echo "---"',
            f'echo "=== Shell Syntax Check ==="',
            f'bash -n {safe_path} 2>&1 && echo "✅ Syntax OK" || echo "❌ Syntax Error"',
        ]
    elif ext in (".js", ".mjs"):
        checks += [
            f'echo "---"',
            f'echo "=== JS Syntax Check ==="',
            f'node --check {safe_path} 2>&1 && echo "✅ Syntax OK" || echo "❌ Syntax Error"',
        ]
    elif ext == ".c":
        checks += [
            f'echo "---"',
            f'echo "=== C Lint ==="',
            f'clang -fsyntax-only -Wall {safe_path} 2>&1 | head -20 || echo "clang not installed"',
        ]

    execute_streaming(handler, " && ".join(checks))


# ── Log Analyzer /log-analyze ────────────────────────────────────────────

def handle_log_analyze(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Read log file and extract errors/warnings."""
    file_path = data.get("file", "").strip()
    if not file_path:
        _json_response(handler, 400, {"error": "Missing 'file' path"})
        return

    safe_path = _shell_quote(file_path)
    cmd = (
        f'echo "Analyzing: {file_path}"; echo "---"; '
        f'echo "Last 200 lines:"; '
        f'tail -200 {safe_path} 2>/dev/null || echo "Cannot read file"; '
        f'echo "---"; '
        f'echo "Errors found:"; '
        f'grep -i -E "error|fail|crash|fatal|exception|traceback|panic|segfault" {safe_path} 2>/dev/null | tail -30 || echo "No errors detected"'
    )
    execute_streaming(handler, cmd)


# ── Script Generator /script-gen ─────────────────────────────────────────

def handle_script_gen(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Create a shell/Python script from description."""
    description = data.get("description", "").strip()
    script_type = data.get("type", "sh").strip()
    output = data.get("output", "").strip()

    if not description:
        _json_response(handler, 400, {"error": "Missing 'description' of what the script should do"})
        return

    safe_out = _shell_quote(output) if output else ""
    if script_type == "py":
        template = f'echo "#!/usr/bin/env python3\\n# Script: {output or "script.py"}\\n# {description}\\n" > {safe_out} && echo "Python template created: {output}"'
    else:
        template = f'echo "#!/data/data/com.termux/files/usr/bin/bash\\n# Script: {output or "script.sh"}\\n# {description}\\n\\nset -e\\n" > {safe_out} && chmod +x {safe_out} && echo "Bash script created: {output}"'

    if output:
        execute_streaming(handler, template)
    else:
        execute_streaming(handler, f'echo "Script described: {description}"\necho "To generate, provide an output path"')


# ── Dependency Tree /deps-tree ───────────────────────────────────────────

def handle_deps_tree(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Show package dependency tree."""
    package = data.get("package", "").strip()
    if package:
        execute_streaming(handler,
            f'echo "=== Reverse depends for: {package} ===" && '
            f'apt-cache rdepends {_shell_quote(package)} 2>/dev/null | head -30 || '
            f'echo "Package not found"'
        )
    else:
        execute_streaming(handler,
            f'echo "=== Installed Packages ===" && '
            f'pkg list-installed 2>/dev/null | wc -l && echo "packages total" && '
            f'echo "---" && '
            f'echo "Top 20 by size:" && '
            f'dpkg-query -W -f=\'${{Installed-Size}}\\t${{Package}}\\n\' 2>/dev/null | sort -rn | head -20 || '
            f'echo "Use dpkg-query for package sizes"'
        )


# ── Storage Auditor /storage-audit ───────────────────────────────────────

def handle_storage_audit(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Find what's eating Termux storage and suggest cleanup."""
    execute_streaming(handler,
        'echo "=== Storage Usage ==="; '
        'df -h /data 2>/dev/null | tail -1; '
        'echo "---"; '
        'echo "Largest directories in ~/:"; '
        'du -sh ~/*/ 2>/dev/null | sort -rh | head -15; '
        'echo "---"; '
        'echo "Cache directories:"; '
        'du -sh ~/.cache/*/ 2>/dev/null | sort -rh | head -10; '
        'echo "---"; '
        'echo "Pip cache:"; '
        'du -sh ~/.cache/pip 2>/dev/null || echo "No pip cache"; '
        'echo "---"; '
        'echo "npm cache:"; '
        'du -sh ~/.npm 2>/dev/null || echo "No npm cache"; '
        'echo "---"; '
        'echo "Trash/Downloads:"; '
        'du -sh ~/storage/downloads/ 2>/dev/null || echo "No downloads folder"; '
        'echo "---"; '
        'echo "💡 Cleanup commands:"; '
        'echo "  pip cache purge"; '
        'echo "  npm cache clean --force"; '
        'echo "  pkg clean"; '
        'echo "  rm -rf ~/.cache/pip"'
    )


# ── Config Doctor /config-fix ────────────────────────────────────────────

def handle_config_fix(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Check and report Termux config issues."""
    which_config = data.get("config", "all").strip()

    checks = []
    if which_config in ("bashrc", "all"):
        checks.append('echo "=== .bashrc ===" && cat ~/.bashrc 2>/dev/null | head -50 || echo "No .bashrc found"')
    if which_config in ("zshrc", "all"):
        checks.append('echo "=== .zshrc ===" && cat ~/.zshrc 2>/dev/null | head -50 || echo "No .zshrc found"')
    if which_config in ("termux", "all"):
        checks.append('echo "=== termux.properties ===" && cat ~/.termux/termux.properties 2>/dev/null || echo "No termux.properties"')
    if which_config in ("font", "all"):
        checks.append('echo "=== Font ===" && ls ~/.termux/font.ttf 2>/dev/null && echo "Custom font found" || echo "Default font"')
    if which_config in ("colors", "all"):
        checks.append('echo "=== colors.properties ===" && cat ~/.termux/colors.properties 2>/dev/null || echo "Default colors"')
    if which_config in ("storage", "all"):
        checks.append('echo "=== Storage Check ===" && ls ~/storage/ 2>/dev/null || echo "Storage NOT set up — run: termux-setup-storage"')
    if which_config in ("path", "all"):
        checks.append(r'echo "=== $PATH ===" && echo "$PATH"')
    if which_config in ("env", "all"):
        checks.append('echo "=== Environment ===" && env | sort | head -30')

    cmd = "\n && ".join(checks) if checks else 'echo "Specify: bashrc, zshrc, termux, font, colors, storage, path, env, or all"'
    execute_streaming(handler, cmd)


# ── Smart Git /git-smart ─────────────────────────────────────────────────

def handle_git_smart(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Git operations with AI-friendly output."""
    action = data.get("action", "diff").strip()
    repo_dir = data.get("repo_dir", get_current_dir()).strip()
    safe_dir = _shell_quote(repo_dir)

    if action == "diff-summary":
        cmd = f'cd {safe_dir} && echo "=== Files Changed ===" && git diff --stat 2>&1 | tail -20 && echo "---" && echo "=== Diff ===" && git diff --unified=5 2>&1 | head -200'
    elif action == "log-recent":
        limit = data.get("limit", 10)
        cmd = f'cd {safe_dir} && git log --oneline --graph -{limit} 2>&1'
    elif action == "suggest-commit":
        cmd = f'cd {safe_dir} && echo "=== Current Changes ===" && git diff --stat 2>&1 && echo "---" && echo "Suggested commit message:" && git diff --cached 2>&1 | head -50 || git diff 2>&1 | head -50'
    elif action == "fix-conflict":
        cmd = f'cd {safe_dir} && echo "=== Conflict Status ===" && git diff --name-only --diff-filter=U 2>&1 && echo "---" && echo "Files with conflicts:" && git diff --check 2>&1 | head -20'
    else:
        cmd = f'cd {safe_dir} && git {action} 2>&1 | tail -30'

    execute_streaming(handler, cmd)


# ── Regex Generator /regex ───────────────────────────────────────────────

def handle_regex(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Test a regex pattern against test strings."""
    pattern = data.get("pattern", "").strip()
    test_str = data.get("test", "").strip()

    if not pattern:
        _json_response(handler, 400, {"error": "Missing 'pattern'"})
        return

    safe_pattern = _shell_quote(pattern)
    if test_str:
        safe_test = _shell_quote(test_str)
        cmd = (
            f'echo "Pattern: {pattern}" && '
            f'echo "Test: {test_str}" && '
            f'echo "---" && '
            f'echo "Matches:" && '
            f'echo {safe_test} | grep -oP {safe_pattern} 2>&1 || echo "No matches or invalid regex"'
        )
    else:
        cmd = f'echo "Pattern: {pattern}" && echo "Provide a test string to check matches"'

    execute_streaming(handler, cmd)


# ── Database Designer /db-design ─────────────────────────────────────────

def handle_db_design(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Create SQLite database from schema description."""
    schema = data.get("schema", "").strip()
    db_path = data.get("output", get_current_dir() + "/database.sqlite").strip()

    if not schema:
        _json_response(handler, 400, {"error": "Missing 'schema' — describe your tables"})
        return

    safe_db = _shell_quote(db_path)
    # Just create the database file and echo back the schema
    cmd = (
        f'touch {safe_db} 2>/dev/null && '
        f'echo "Database created: {db_path}" && '
        f'echo "---" && '
        f'echo "Schema description:" && '
        f'echo "{schema}" && '
        f'echo "---" && '
        f'echo "Use /db-query to run SQL. Example: CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"'
    )
    execute_streaming(handler, cmd)


# ── Termux Backup /backup ────────────────────────────────────────────────

def handle_backup(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Backup Termux home directory or specified paths."""
    target = data.get("target", "home").strip()
    output = data.get("output", "").strip()
    include = data.get("include", "").strip().split(",") if data.get("include", "").strip() else []

    import time
    home = os.environ.get("HOME", "/data/data/com.termux/files/home")
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    if not output:
        output = f"~/storage/shared/termux_backup_{timestamp}.tar.gz"

    safe_out = _shell_quote(output)

    if target == "home":
        cmd = (
            f'mkdir -p "$(dirname {safe_out})" 2>/dev/null; '
            f'echo "Backing up: {home}"; '
            f'echo "Output: {output}"; '
            f'echo "---"; '
            f'tar -czf {safe_out} -C "$(dirname {home})" "$(basename {home})" --exclude=".cache" --exclude="__pycache__" --exclude="node_modules" --exclude="*.pyc" 2>&1 && '
            f'echo "✅ Backup complete!" && '
            f'ls -lh {safe_out} || echo "❌ Backup failed"'
        )
    elif target == "packages":
        cmd = (
            f'echo "Backing up package list..." && '
            f'pkg list-installed > {safe_out} 2>&1 && '
            f'echo "✅ Package list saved to: {output}" && '
            f'echo "Restore with: xargs pkg install -y < {safe_out}"'
        )
    elif target == "configs":
        cmd = (
            f'echo "Backing up configs..." && '
            f'tar -czf {safe_out} ~/.bashrc ~/.zshrc ~/.termux/ ~/.config/ 2>/dev/null && '
            f'echo "✅ Configs saved to: {output}" && '
            f'ls -lh {safe_out}'
        )
    elif include:
        inc_paths = " ".join(_shell_quote(p.strip()) for p in include if p.strip())
        cmd = (
            f'mkdir -p "$(dirname {safe_out})" 2>/dev/null; '
            f'echo "Backing up selected paths..." && '
            f'tar -czf {safe_out} {inc_paths} 2>&1 && '
            f'echo "✅ Backup complete: {output}" && '
            f'ls -lh {safe_out} || echo "❌ Backup failed"'
        )
    else:
        cmd = 'echo "Targets: home, packages, configs. Or specify include: path1,path2"'

    execute_streaming(handler, cmd)


# ── Termux Restore /restore ──────────────────────────────────────────────

def handle_restore(handler: "BaseHTTPRequestHandler", data: dict) -> None:
    """Restore from a Termux backup file."""
    backup_file = data.get("file", "").strip()
    target = data.get("target", "home").strip()

    if not backup_file:
        _json_response(handler, 400, {"error": "Missing 'file' — path to backup archive"})
        return

    safe_file = _shell_quote(backup_file)
    home = os.environ.get("HOME", "/data/data/com.termux/files/home")

    if target == "home":
        cmd = (
            f'echo "⚠️ WARNING: This will overwrite files in {home}"; '
            f'echo "Restoring from: {backup_file}"; '
            f'echo "---"; '
            f'tar -xzf {safe_file} -C "$(dirname {home})" 2>&1 && '
            f'echo "✅ Restore complete!" || echo "❌ Restore failed"'
        )
    elif target == "packages":
        cmd = (
            f'echo "Restoring packages from: {backup_file}"; '
            f'xargs pkg install -y < {safe_file} 2>&1 | tail -20 && '
            f'echo "✅ Packages restored!" || echo "❌ Restore failed"'
        )
    elif target == "configs":
        cmd = (
            f'echo "Restoring configs from: {backup_file}"; '
            f'tar -xzf {safe_file} -C {_shell_quote(home)} 2>&1 && '
            f'echo "✅ Configs restored!" || echo "❌ Restore failed"'
        )
    elif target == "info":
        cmd = (
            f'echo "=== Backup Info ===" && '
            f'echo "File: {backup_file}" && '
            f'tar -tzf {safe_file} 2>/dev/null | head -50 && '
            f'echo "---" && '
            f'tar -tzf {safe_file} 2>/dev/null | wc -l && echo "files total"'
        )
    else:
        cmd = 'echo "Targets: home, packages, configs, info"'

    execute_streaming(handler, cmd)
