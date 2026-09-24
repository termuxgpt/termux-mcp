# Termux-MCP

A lightweight HTTP server that runs inside Termux on Android, exposing shell execution and device capabilities as a streaming API. Built for AI agents, LLM tool-calling, and automation scripts.

```text
AI Agent --> POST /run {"cmd": "ls -la"} --> Termux-MCP --> Termux Shell
                                                  |
                      <-- chunked streaming output -+
```

## Two commands — which do I run?

| Command | What it is |
|---|---|
| `termux-mcp` | REST API daemon on `:8080` (this README's endpoints) |
| `termux-native-mcp` | **Native MCP server** (Model Context Protocol, spec 2025-06-18) for MCP clients: RikkaHub, desktop AI apps (Cursor, ...) |

Both install together via `pkg install termux-mcp` and run as **separate
processes** — starting `termux-native-mcp` never touches the REST daemon's
port, state or behavior.

```
termux-mcp                  # REST API on 127.0.0.1:8080
termux-native-mcp           # native MCP, Streamable HTTP on 127.0.0.1:8081
termux-native-mcp --stdio   # native MCP over stdin/stdout (desktop clients)
```

RikkaHub (same phone):

```json
{
  "type": "http",
  "url": "http://127.0.0.1:8081/mcp",
  "commonOptions": {
    "name": "Termux",
    "enable": true,
    "headers": [["Authorization", "Bearer <TERMUX_MCP_AUTH_TOKEN>"]]
  }
}
```

Desktop MCP client (over ssh): `{"command": "ssh", "args": ["android", "termux-native-mcp", "--stdio"]}`

Full guide, env vars and tool list: **[docs/mcp.md](docs/mcp.md)**.
Both servers share the same safety system (risk gates, file snapshots,
trash) because they run the same handler code.

## Installation

### pip (recommended)

```bash
pkg update && pkg install python -y
pip install termux-mcp
termux-mcp
```

This installs a global `termux-mcp` command — it runs from any directory, and
`pip install -U termux-mcp` upgrades it.

### From a clone

```bash
pkg update && pkg install python git -y
git clone https://github.com/termuxgpt/termux-mcp
cd termux-mcp
pip install .
termux-mcp
```

`pip install .` registers the command globally, so the `cd` is only needed for
the install itself. Without it, `python -m termux_mcp` only works from inside
the clone.

### Package repository

```bash
curl -fSL https://termux-mcp.pages.dev/add-repo.sh | bash
pkg install termux-mcp
termux-mcp
```

## Quick Test

```bash
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/run -H "Content-Type: application/json" -d '{"cmd": "ls ~"}'
```

## Termux Tutorial Apps (AD)
<center><a href="https://bit.ly/termuxtoolbox"><img src="https://raw.githubusercontent.com/Bhai4You/bhai4you/refs/heads/master/termux_toolbox_app_banner.png" alt="Termux Toolbox"  ></a></br><a href="https://play.google.com/store/apps/details?id=com.codeninja.termuxbannerx"> <img src="https://raw.githubusercontent.com/Bhai4You/bhai4you/refs/heads/master/termux-bannerx.png" alt="Termux BannerX"  ></a> <a href="https://play.google.com/store/apps/details?id=com.codeninja.termuxtutor"><img src="https://raw.githubusercontent.com/Bhai4You/bhai4you/refs/heads/master/termux-tutor.png" alt="Termux Tutor"  ></a></center></br>

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TERMUX_MCP_PORT` | `8080` | HTTP listen port |
| `TERMUX_MCP_HOST` | `127.0.0.1` | Bind address. Use `127.0.0.1` for local-only. |
| `TERMUX_MCP_TIMEOUT` | `0` | Command timeout in seconds. `0` = **no timeout** (default) — long operations like `pkg upgrade` run until they finish. Set a positive value to re-enable the watchdog kill. |
| `TERMUX_MCP_AUTH_TOKEN` | (none) | Bearer token for authentication |
| `TERMUX_MCP_MAX_OUTPUT` | `20000` | Max streamed output bytes per command. Output beyond this is drained (process keeps running) but not sent; a truncation marker is appended. Keeps LLM tool results small and token-efficient. |

Set `TERMUX_MCP_AUTH_TOKEN` to a value 16+ characters long to require authentication on all endpoints. When binding to a non-loopback address, authentication is mandatory.

## Endpoints

### Shell & Filesystem

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/run` | POST | `cmd` (string, required) | Execute a shell command with streaming output. The working directory is process-wide and shared between clients over HTTP — chain `cd x && cmd` for isolation. |
| `/ls` | POST | `path` (string, default `.`), `detailed` (bool) | List directory contents |
| `/read` | POST | `path` (string, required) | Read a file (first 500 lines) |
| `/write` | POST | `path`, `content` | Write content to a file via base64 encoding |
| `/mkdir` | POST | `path` | Create directory (`mkdir -p`) |
| `/delete` | POST | `path`, `recursive` (bool), `confirmed` (bool) | Delete file or directory. Requires confirmation. |
| `/search` | POST | `path`, `pattern` (or `query`/`name`) | Find files by name pattern |
| `/cancel` | POST | | Cancel currently running command |

### System Monitor & Management

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/system-info` | POST | | Live CPU%, RAM, disk, temperature, uptime as JSON |
| `/process-list` | POST | `limit` (int, default 20) | List running processes sorted by CPU usage |
| `/process-kill` | POST | `pid` (int), `signal` (int, default 15) | Terminate a process by PID |
| `/health` | POST | | Full diagnostic: core packages, Termux:API, storage, network, permissions |

### Cron Scheduler

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/cron-add` | POST | `schedule`, `command`, `label` | Add a cron job. Schedule format: `0 3 * * *` for daily at 3am. |
| `/cron-list` | POST | | List all cron jobs |
| `/cron-remove` | POST | `label` (optional) | Remove cron jobs matching a label, or all if no label given |

### Backup, Restore & Cloud Sync

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/backup` | POST | `target` (home/packages/configs), `output`, `include` | Create a tar.gz backup of home, packages, or configs |
| `/restore` | POST | `file`, `target` | Restore from a backup file |
| `/cloud-sync` | POST | `action` (backup/restore/list), `target`, `output`, `file` | Create backups and provide cloud upload instructions |
| `/changes_list` | POST | `format` (json/text), `limit`, `since`, `task_id` | Every file write, delete and trash the safety layer recorded, newest first, with a `revertable` flag |
| `/undo` | POST | `path`, `limit`, `since`, `task_id`, `confirmed` | Put files back to their earlier contents from the snapshots. Needs `confirmed: true`, since an undo is itself undone by taking a snapshot first |

### Code & Files

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/diff` | POST | `file`, `file2` (optional) | Show diff between files, or file stats for a single file |
| `/patch` | POST | `file`, `patch` | Apply a diff patch to a file |

### Device & Sensors

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/battery` | POST | | Battery status via `termux-battery-status` |
| `/location` | POST | `provider` (gps/network, default gps) | GPS coordinates via `termux-location` |
| `/wifi-info` | POST | | WiFi connection details |
| `/wifi-scan` | POST | | Scan nearby WiFi networks |
| `/camera-photo` | POST | `camera_id` (0/1), `output` | Take a photo |
| `/camera-info` | POST | | List available cameras |
| `/screenshot` | POST | `output` | Take a screenshot |
| `/sensor` | POST | `sensor`, `limit` | Read sensor data |
| `/fingerprint` | POST | | Fingerprint authentication |
| `/vibrate` | POST | `duration_ms` | Vibrate the device |
| `/torch` | POST | `state` (on/off) | Toggle flashlight |
| `/brightness` | POST | `level` | Get/set screen brightness |
| `/volume` | POST | `stream`, `level` | Get/set volume |

### Communication

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/notify` | POST | `title`, `content`, `priority`, `id` | Send an Android notification |
| `/notify-remove` | POST | `id` | Remove a notification |
| `/sms-send` | POST | `number`, `text` | Send an SMS |
| `/sms-inbox` | POST | `limit` | Read SMS inbox |
| `/tts-speak` | POST | `text`, `rate`, `pitch` | Text-to-speech |
| `/speech-to-text` | POST | | Speech recognition |
| `/toast` | POST | `text` | Show an Android toast |
| `/dialog` | POST | `title`, `message` | Show a confirmation dialog |
| `/share` | POST | `text` or `file` | Share via Android intent |
| `/clipboard-get` | POST | | Read clipboard |
| `/clipboard-set` | POST | `text` | Set clipboard |
| `/call` | POST | `number` | Initiate a phone call |
| `/contacts` | POST | | List contacts |
| `/list-apps` | POST | | List installed apps |

### Network

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/open-url` | POST | `url` | Open URL in browser |
| `/download` | POST | `url`, `description`, `title` | Download a file |
| `/public-ip` | POST | | Get public IP address |
| `/weather` | POST | `city` | Weather via wttr.in |
| `/speedtest` | POST | | Internet speed test |
| `/web-server` | POST | `action` (start/stop/status), `port`, `directory` | Start a Python HTTP server |

### Media

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/image-process` | POST | `action`, `input`, `output`, `width`, `height` | Image operations via ImageMagick |
| `/video-process` | POST | `action`, `input`, `output`, `crf`, `start`, `duration` | Video operations via FFmpeg |
| `/text-extract` | POST | `input`, `lang` | OCR via Tesseract |
| `/qrcode` | POST | `text`, `output` | Generate QR code |
| `/scan-barcode` | POST | `camera_id`, `output` | Scan barcode via camera |
| `/screen-record` | POST | `output`, `action` (start/stop) | Screen recording |
| `/microphone-record` | POST | `output`, `limit_seconds`, `action` | Microphone recording |
| `/wallpaper` | POST | `file`, `lockscreen` | Set wallpaper |

### Smart Tools

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/smart-install` | POST | `packages`, `manager`, `dry_run` | Intelligent package install with conflict detection |
| `/diagnose` | POST | `intent` (python/pip/node/git/storage/packages/all) | Run diagnostics for a specific tool |
| `/pkg-smart` | POST | `intent`, `install` | Intent-based package discovery (60+ mappings) |
| `/dev-env` | POST | `intent`, `name` | One-click development environment setup |
| `/profile` | POST | `profile`, `dry_run` | Pre-configured Termux profiles (dev, python, web, hacker, etc.) |
| `/optimize` | POST | | Performance analysis and recommendations |
| `/error-explain` | POST | `error`, `command` | Gather context to help AI explain errors |
| `/permission-fix` | POST | `target` | Diagnose and fix permission issues |
| `/storage-audit` | POST | | Find large files and suggest cleanup |
| `/deps-tree` | POST | `package` | Show package dependency tree |
| `/config-fix` | POST | `config` | Check Termux configuration issues |
| `/review` | POST | `file` | Static analysis (syntax check, linting) |
| `/log-analyze` | POST | `file` | Extract errors and warnings from log files |
| `/script-gen` | POST | `description`, `type`, `output` | Generate shell or Python script templates |
| `/regex` | POST | `pattern`, `test` | Test regex patterns with grep |
| `/db-design` | POST | `schema`, `output` | Create SQLite database from schema description |
| `/db-query` | POST | `database`, `query` | Execute SQLite query |
| `/translate` | POST | `text`, `target_lang`, `source_lang` | Text translation |
| `/tutorial` | POST | `topic` | Interactive Termux learning guide |

### Git Operations

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/git-op` | POST | `action` (clone/status/log/diff/pull/push/branch), `url`, `directory`, `repo_dir` | General git operations |
| `/git-smart` | POST | `action` (diff-summary/log-recent/suggest-commit/fix-conflict), `repo_dir` | AI-friendly smart git operations |

### SSH, Services & Migration

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/ssh-wizard` | POST | `action` (setup/status/stop) | Full SSH server setup with key generation |
| `/service-guard` | POST | `action`, `name`, `cmd` | Background service management |
| `/history-insight` | POST | `file`, `limit` | Analyze shell usage patterns, suggest aliases |
| `/quick-cmd` | POST | `action`, `name`, `cmd` | Alias and shortcut management |
| `/port-manage` | POST | `action`, `port` | Network port visibility |
| `/migrate` | POST | `action` (backup/restore/preview), `output`, `file` | Full Termux environment migration |

### Other

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/ping` | GET | | Health check |
| `/approve` | POST | `action`, `reason`, `method` | Ask the user to approve one exact action on the device — their fingerprint, or a dialog when no fingerprint is enrolled. An approved action runs once, within 3 minutes |
| `/ask` | POST | `widget`, `title`, `hint`, `values`, `multiline`, `format` | Ask the user for input with a native Android dialog: text, number, radio, sheet, spinner, checkbox, date, time or speech. `values` are the choices; commas inside one are fine, backslashes are not |
| `/tools` | GET | | Full OpenAI-format tool schemas for all tools (function-calling ready) |
| `/catalog` | GET | | Compact tool catalog: `{name, desc, params, category}` per tool — small enough to embed in an LLM system prompt or a `use_tool` meta-tool |
| `/env` | GET | | Environment info (cwd, home, pid) |
| `/explain` | POST | `cmd` | Explain what a shell command does |
| `/telephony-deviceinfo` | POST | | Device telephony info |
| `/telephony-cellinfo` | POST | | Cell tower info |
| `/infrared` | POST | `frequency`, `pattern` | IR blaster |
| `/media-player` | POST | `action` | Media playback control |
| `/storage-get` | POST | `output` | Get file via Android SAF |

## Streaming Output

The `/run` endpoint and most tool endpoints use HTTP chunked transfer encoding. Output is sent line-by-line as the command produces it. Clients should read the response as a stream and process each chunk as it arrives.

For long-running commands, a watchdog thread enforces the timeout. Package install commands automatically receive `-y` flags and `DEBIAN_FRONTEND=noninteractive` to prevent prompts.

## Security

- **Command injection.** Every parameter interpolated into a shell command is
  quoted with `shell_quote`, and every numeric parameter is validated by
  `require_number`/`require_int` — a value that is not a number is rejected,
  not passed through. Parameters embedded in double-quoted `echo` arguments
  are quoted as whole strings, since double quotes still permit `$(...)`.
- **Risk gate.** Commands are assessed before execution. The `DANGEROUS` tier
  (e.g. `rm -rf /`, `mkfs.`, writes to `/dev/`, `chmod -R 777`) is refused on
  every shell-backed endpoint — the check lives in the shared executor, so a
  new endpoint cannot forget it. The `WARNING` tier (package removals,
  recursive deletes) requires an explicit `confirmed: true` on `/run`, or an
  approval for that exact command taken from the device with `/approve` —
  which costs the user a fingerprint instead of a tap.
- **Sensitive writes.** `/write` and `/patch` can edit anything outside
  `/dev`, `/proc` and `/sys` — that is the point of them — but paths that
  grant persistence require confirmation: `~/.ssh/`, `~/.bashrc`,
  `~/.profile`, `~/.termux/` (including `boot/`), and anything under
  `$PREFIX`. These are an SSH backdoor, code run on shell start, and code run
  at device boot respectively.
- **Authentication.** With `TERMUX_MCP_AUTH_TOKEN` set, every endpoint
  requires a Bearer token — POST, GET (except `/ping`, which the client's
  connectivity probe needs before it holds a token), and the WebSocket, which
  accepts the token via the `Authorization` header or a `?token=` query
  parameter. Non-loopback binding enforces mandatory authentication.
- **Network.** A request carrying an `Origin` header is rejected unless that
  origin is allowlisted, so a page open on the device cannot reach the server
  by resolving its own hostname to loopback. Requests with no `Origin` (the
  app, `curl`, stdio clients) are unaffected.
- Request body size is capped at 5 MB; WebSocket frames at 8 MB.
- Request bodies are not logged — only their size.
- Each WebSocket connection keeps its own `cd` state. Over HTTP the working
  directory is **process-wide and shared between clients**, so one caller's
  `cd` affects the next; chain `cd x && cmd` if you need isolation.


## License

**GNU Affero General Public License v3.0 (AGPL-3.0)**

Copyleft — anyone who distributes a modified version, or hosts it as a
service (SaaS), must release their changes under the same license.

Note: versions up to 0.8.3 were released under the MIT License; those
releases keep their MIT terms. AGPL-3.0 applies from 0.8.4 onward.
