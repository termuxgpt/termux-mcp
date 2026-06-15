# Termux-MCP

A lightweight HTTP server that runs inside Termux on Android, exposing shell execution and device capabilities as a streaming API. Built for AI agents, LLM tool-calling, and automation scripts.

```text
AI Agent --> POST /run {"cmd": "ls -la"} --> Termux-MCP --> Termux Shell
                                                  |
                      <-- chunked streaming output -+
```

## Installation

```bash
pkg update && pkg install python git -y
git clone https://github.com/termuxgpt/termux-mcp
cd termux-mcp
python -m termux_mcp
```

Or via the package repository:

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

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TERMUX_MCP_PORT` | `8080` | HTTP listen port |
| `TERMUX_MCP_HOST` | `127.0.0.1` | Bind address. Use `127.0.0.1` for local-only. |
| `TERMUX_MCP_TIMEOUT` | `120` | Command timeout in seconds |
| `TERMUX_MCP_AUTH_TOKEN` | (none) | Bearer token for authentication |

Set `TERMUX_MCP_AUTH_TOKEN` to a value 16+ characters long to require authentication on all endpoints. When binding to a non-loopback address, authentication is mandatory.

## Endpoints

### Shell & Filesystem

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `/run` | POST | `cmd` (string, required) | Execute a shell command with streaming output. Maintains persistent `cd` state across requests. |
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

- All commands are checked against a risk assessment system. Dangerous patterns (like `rm -rf /` or writes to `/dev/`) are blocked.
- Paths targeting `/dev/`, `/proc/`, or `/sys/` are rejected with canonical path resolution.
- Numeric parameters are validated before shell interpolation to prevent injection.
- When `TERMUX_MCP_AUTH_TOKEN` is set, all POST endpoints require a Bearer token.
- Non-loopback binding enforces mandatory authentication.
- Request body size is capped at 5 MB.


## License

MIT
