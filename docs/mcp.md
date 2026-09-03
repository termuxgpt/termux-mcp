# Native MCP server (`termux-native-mcp`)

`termux-native-mcp` speaks the **Model Context Protocol** (spec 2025-06-18)
natively — no REST, no adapters. It exposes the same tool catalog and the
same safety system as the REST API (`termux-mcp`), but through JSON-RPC
transports that MCP clients understand:

* **Streamable HTTP** (default) — for [RikkaHub](https://rikkahub.me) and
  any network MCP client.
* **stdio** — for desktop AI apps (Cursor, ...) that spawn a command,
  usually over `ssh android`.

Both commands are installed by the same `pkg install termux-mcp`. They are
**independent processes** — running `termux-native-mcp` never touches the
REST server's port, state, or behavior.

```
termux-mcp              REST API daemon on :8080   (unchanged, separate)
termux-native-mcp       native MCP — Streamable HTTP on 127.0.0.1:8081
termux-native-mcp --stdio   native MCP over stdin/stdout
termux-native-mcp --port 9000 --host 0.0.0.0
```

## Quick start (RikkaHub, same phone)

```bash
termux-native-mcp
```

RikkaHub → Add MCP server:

```json
{
  "type": "http",
  "url": "http://127.0.0.1:8081/mcp",
  "commonOptions": {
    "name": "Termux",
    "enable": true,
    "headers": [["Authorization", "Bearer <your-token>"]]
  }
}
```

(the `Authorization` header is only needed once you set a token — see
below).

## Quick start (desktop client, from a computer)

On the phone, make sure `sshd` runs (`pkg install openssh`). Then in the
desktop client's MCP config:

```json
{ "mcpServers": {
    "termux": { "command": "ssh",
                "args": ["android", "termux-native-mcp", "--stdio"] }
} }
```

Verify the wire works before configuring a client:

```bash
curl -s http://127.0.0.1:8081/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18"}}'
```

## What you get

57 tools (curated from the REST catalog, duplicates merged): shell `run`
with persistent `cd` per session, filesystem with snapshot/trash safety,
device & sensors via termux-api, media, cron, git, smart installs — plus
MCP-native tools:

| Tool | Why it exists |
|---|---|
| `run` | shell commands; `confirmed: true` acknowledges risk warnings |
| `cancel` | kill the command currently running in your session |
| `session_start` / `session_run` / `session_poll` / `session_list` / `session_kill` | long jobs (e.g. `pkg upgrade`) run in a tmux session: `session_run` returns immediately, `session_poll` fetches new output — don't hold a `tools/call` open for minutes |

Every safety guarantee of the REST API carries over, because both servers
run the same handler code: dangerous commands are blocked, risky ones need
`confirmed: true`, file overwrites are snapshotted under
`~/termuxGPT/snapshots/`, deletes move to `~/termuxGPT/trash/`.

### Long-running commands

A `tools/call` returns only when the command finishes. For anything long:

1. `session_start`
2. `session_run` `{cmd: "pkg upgrade"}`
3. `session_poll` (repeat until `running: false`)

If a client passes `_meta.progressToken` on a call, the server also emits
`notifications/progress` with live output lines (up to 200 per call).

## Configuration

| Env var / flag | Default | Meaning |
|---|---|---|
| `TERMUX_NATIVE_MCP_PORT` / `--port` | `8081` | HTTP listen port |
| `TERMUX_NATIVE_MCP_HOST` / `--host` | `127.0.0.1` | Bind address. Non-loopback **requires** an auth token. |
| `TERMUX_NATIVE_MCP_AUTH_TOKEN` | (fallback `TERMUX_MCP_AUTH_TOKEN`) | Bearer token. ≥ 16 chars, refused otherwise. |
| `TERMUX_NATIVE_MCP_PATH` | `/mcp` | MCP endpoint path |
| `TERMUX_NATIVE_MCP_ORIGIN` | (empty) | Allowed `Origin` values, comma-separated (DNS-rebinding guard) |
| `TERMUX_NATIVE_MCP_TOOLS` | (all) | Tool filter: `run,ls,-history` |
| `TERMUX_NATIVE_MCP_SESSION_TTL` | `1800` | Idle HTTP session expiry (seconds) |
| `--stdio` | — | stdio transport instead of HTTP |

stdio mode inherits the launching process's environment: if auth is on,
set `TERMUX_NATIVE_MCP_AUTH_TOKEN` for the ssh command too.

## Security model

* Bind loopback by default; non-loopback binding without an auth token
  refuses to start (same rule as the REST server).
* Bearer auth (constant-time compare) on every HTTP request, including the
  SSE streams.
* `Origin` validated against `TERMUX_NATIVE_MCP_ORIGIN`; with no allowlist
  configured, Origins are only accepted on loopback connections.
* Every request in a session carries `Mcp-Session-Id` (issued at
  `initialize`); unknown/expired sessions get 404 so clients re-initialize.
* Sessions expire after `TERMUX_NATIVE_MCP_SESSION_TTL` idle seconds;
  running commands are never killed by TTL — only by `cancel`, session
  DELETE, or server shutdown.
* Same risk gates, path blocking (`/dev /proc /sys`), snapshots and trash
  as the REST API — both servers share the handler layer.
