<div align="center">
  
  # Termux MCP API Reference
</div>

## Base URL

```
http://<device-ip>:8080
```
---

## GET `/ping`

Health check. Returns server status and current working directory.

**Response**
```json
{
  "status": "ok",
  "cwd": "/data/data/com.termux/files/home"
}
```

---

## POST `/run`

Execute a shell command. Output is streamed in real-time.

**Request Body**
```json
{
  "cmd": "ls -la"
}
```

**Response** — `Transfer-Encoding: chunked` · `Content-Type: text/plain`

Each chunk is a line of terminal output as it's produced.

---

### Special Commands

| Command    | Behavior                                            |
|------------|-----------------------------------------------------|
| `cd <dir>` | Changes working directory, persists across requests |
| `cd`       | Returns to `$HOME`                                  |
| `cd ~`     | Same as `cd`                                        |

---

### Error Responses

| Status | Meaning                      |
|--------|------------------------------|
| `400`  | Missing or empty `cmd` field |
| `404`  | Unknown route                |
| `500`  | Unexpected server error      |


