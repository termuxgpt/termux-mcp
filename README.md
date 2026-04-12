<div align="center">
<a href="https://play.google.com/store/apps/details?id=com.codeninja.termuxtutor"><img src="https://raw.githubusercontent.com/Bhai4You/bhai4you/refs/heads/master/termux-mcp.png" alt="Termux Tutor"  ></a>
   

A lightweight HTTP server that exposes Termux shell as a streaming API —
built to pair with AI agents, Claude, or Postman.

[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Termux%20%7C%20Android-green?style=flat-square&logo=android)](https://termux.dev)
[![License](https://img.shields.io/badge/License-MIT-blue?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen?style=flat-square)]()

</div>

---

## 📖 What is TermuxMCP?

TermuxMCP runs inside [Termux](https://termux.dev) and provides a simple HTTP interface so any AI agent can:

- 🖥️ Execute shell commands with **real-time streaming output**
- 📂 Navigate directories with **persistent `cd` state**
- 📦 Install packages **non-interactively** (`pkg install`, `apt install`)
- 💓 Check server health via `/ping`

```
AI / API Call ──►  POST /run {"cmd": "ls -la"}  ──►  TermuxMCP  ──►  Termux Shell
                                                         │
                         ◄── chunked streaming output ───┘
```

---

## Quick Start

### 1 — Install in Termux

```bash
pkg update && pkg install python git -y;git clone https://github.com/Bhai4You/termux-mcp;cd termux-mcp;python -m termux_mcp
```

### 2 — Test it

```bash
# Health check
curl http://localhost:8080/ping

# Run a command
curl -X POST http://localhost:8080/run \
     -H "Content-Type: application/json" \
     -d '{"cmd": "ls ~"}'

# Install a package (streaming output)
curl -X POST http://localhost:8080/run \
     -H "Content-Type: application/json" \
     -d '{"cmd": "pkg install python"}'
```

### 3 — Custom port

```bash
TERMUX_MCP_PORT=9090 python -m termux_mcp
```

---

## API Reference

| Method | Path    | Body                   | Response                       |
|--------|---------|------------------------|--------------------------------|
| GET    | `/ping` | —                      | `{"status":"ok","cwd":"..."}` |
| POST   | `/run`  | `{"cmd": "<shell>"}` | Chunked plain-text stream      |

### Streaming Response Format

Output uses HTTP chunked transfer encoding (`Transfer-Encoding: chunked`).  
Read it line by line — each line is real terminal output **as it happens**.

---

## Configuration

All settings can be overridden via environment variables:

| Variable          | Default     | Description           |
|-------------------|-------------|-----------------------|
| `TERMUX_MCP_PORT` | `8080`      | HTTP listen port      |
| `TERMUX_MCP_HOST` | `0.0.0.0`   | Bind address          |
| `HOME`            | Termux home | Working directory base|

