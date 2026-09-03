# Native MCP Support — Code Audit & Implementation Design

**Issue:** termuxgpt/termux-mcp#3 — "Add native MCP (Model Context Protocol) server support"
**Audit target:** `main` @ `dd812fe` (v0.9.0) — verified identical to `origin/main` (clean tree, 0/0 ahead/behind), so this audit covers the live release.
**Constraint honored throughout:** zero changes to the working REST API. Every MCP component below is a *new* file or an *additive* packaging entry; no existing module is edited except `__main__.py`/`pyproject.toml` in the optional same-process phase, and those edits are behavior-neutral when MCP is not enabled.

---

## 1. Executive summary

termux-mcp is **not an MCP server today** — it is a hand-rolled HTTP/WebSocket RPC server that *happens to be named* "mcp". Adding true MCP (JSON-RPC 2.0 over stdio and Streamable HTTP, per the 2025-06-18 spec) is very feasible **without touching the REST layer**, because:

1. Every one of the ~110 HTTP endpoints funnels through two tiny primitives (`execute_streaming(handler, cmd)` in `shell.py` and `json_response(handler, status, data)` in `utils.py`) that only touch a 5-member HTTP surface (`send_response`, `send_header`, `end_headers`, `wfile`, `log_message`).
2. The WebSocket transport (`websocket.py`, ~960 lines) is already a *second protocol* layered beside REST with per-connection state — it is the exact architectural precedent an MCP transport should follow.
3. The OpenAI-format tool schemas in `tools_schema.py` are already JSON-Schema-compatible inside (`function.parameters`), so `tools/list` is mostly a re-shape, not a rewrite.

**Recommended shape (option A from the issue):** dual-mode server — REST stays exactly as-is on port 8080; a Streamable-HTTP MCP endpoint runs on a *separate port* (`/mcp`), plus an optional stdio mode for desktop clients. MCP is **opt-in** (new env var / flag / console script), so the live service is untouched unless enabled.

**Headline audit findings that shape the design** (details in §2):

| # | Finding | Consequence for MCP |
|---|---|---|
| A1 | Tool logic exists in **two drifting copies**: REST handlers (`handler.py` + `handlers/*`) and WS builders (`websocket.py`). WS `delete` runs raw `rm -rf` (no trash), WS `write` takes **no snapshot** — the REST paths are the safe ones. | MCP must dispatch to the **REST handler layer**, never copy WS cmd-strings. |
| A2 | HTTP "persistent `cd`" is per-**thread** and `ThreadingHTTPServer` spawns a fresh thread per request → `cd` does not actually persist across HTTP requests, and `/cancel` + `/env`'s `active_command_pid` can never work over HTTP (each request thread has its own empty thread-local). | MCP must use WS-style **per-session state** (own cwd, own pid, own kill flag), not the REST thread-local model. |
| A3 | `OPENAI_TOOLS` lists **61** tools but the server routes ~110 endpoints, with stale duplicates (`camera` vs `camera_photo`, `wifi` vs `wifi_info`, `sms` vs `sms_send`, `tts` vs `tts_speak`, `ocr`; plus WS-only `session_*`, `history_*`). | MCP `tools/list` must expose a curated, unique set (clients choke on 100+ tools / token bloat). Define profiles. |
| A4 | Every handler writes either chunked text (command output) or JSON, both with a distinctive HTTP shape. Success/error semantics are encoded in `❌ Exit code: N` / `✅ Done` markers and HTTP statuses. | MCP result mapping rules must decode these into `content` + `isError`. |
| A5 | Confirmation flow is **client-round-trip** (`confirmed: true` re-send) — works fine over MCP because it's already an in-schema parameter. | Keep identical semantics; blocked = `isError`, confirmation-required = structured prompt to re-call. |
| A6 | Risk gates (`security.py`), file safety (`safety.py`), auto-`-y`, output caps, `DEBIAN_FRONTEND` injection all live **inside** `execute_streaming`/`_run_process` and the handler bodies. | Reuse is automatic once dispatch targets the REST handler methods. |

---

## 2. Audit findings (full)

### 2.1 Architecture map

```
__main__.run() ──► server.run()
                    ├─ ThreadingHTTPServer(HOST:8080, MCPHandler)   [stdlib http.server, 1 thread/request]
                    ├─ kill_port(8080)   network.py  (lsof kill + poll)
                    └─ /ws route ─► websocket.ws_handler()           [hand-rolled RFC6455, per-conn state]
MCPHandler.do_POST: ~110 path → method/router table
   ├─ inline _handle_* methods  (handler.py)     ──► execute_streaming / json_response
   └─ handlers/{terminal,features,ai_power,history}.py  (all take (handler, data))
execute_streaming(handler, cmd)  shell.py
   ├─ per-THREAD state: cwd, active_pid, pid_lock   (threading.local)
   ├─ cd handling (persistent only per-thread)
   ├─ preprocess: pkg/apt -y injection + DEBIAN_FRONTEND=noninteractive
   ├─ Popen("export PAGER=cat; <cmd>", shell=True, setsid, cwd=tld.cwd)
   ├─ optional timeout watchdog (TERMUX_MCP_TIMEOUT > 0)
   ├─ line-loop → chunked HTTP frames, MAX_OUTPUT_BYTES cap + truncation marker
   └─ trailing "❌ Exit code: N" / "✅ Done" markers
safety.py: snapshot_before_write / trash_path / snapshot_targets_from_command   (used by /write,/delete,/run)
security.py: get_risk_assessment → blocked / requires_confirmation
websocket.py: per-connection {cwd, active_pid, killed Event, lock, send_lock}
   _ws_execute_tool(tool, params, conn, req_id) → builds cmd strings AGAIN → _ws_run_process
tools_schema.py: OPENAI_TOOLS (61, OpenAI "function" format) + build_catalog()
```

### 2.2 Safety model (the part MCP must inherit, not reimplement)

- `security.py` — regex risk assessment: `DANGEROUS_PATTERNS` → hard block; `WARNING_PATTERNS` → `requires_confirmation`. Applied in `_handle_run` only (and per-handler path checks).
- `utils.is_safe_path` — canonical-path block of `/dev/ /proc/ /sys/`; applied by file handlers.
- `safety.py` — writes: `snapshot_before_write` copies the target to `~/termuxGPT/snapshots/<ts>/` and *echoes the snapshot path* so the AI can restore; deletes move to `~/termuxGPT/trash/`; `/run` gets a best-effort `snapshot_targets_from_command` scan (redirects, tee, sed -i, cp/mv, truncate, dd of=) prepended as an echo hint.
- Output cap `MAX_OUTPUT_BYTES` (20 KB default) with truncation marker; auto-yes only for install commands; `preexec_fn=os.setsid` so children are killable process groups.

**Audit finding (security drift):** the WS tool layer **bypasses** trash/snapshot — WS `delete` executes raw `rm -rf` (`websocket.py:388`), WS `write` overwrites without snapshot (`websocket.py:371`), and WS applies none of `security.py`'s risk gates. This drift is exactly what an MCP layer must not copy. (Recommendation for a *later* cleanup issue: delete the WS cmd-string builders and route WS through the same handler layer.)

### 2.3 State model — where REST is weakest

`shell.py` keeps cwd/active-pid in `threading.local`. Under `ThreadingMixIn` every request is a brand-new thread, so:

- `cd ~/foo` sets the *current request thread's* cwd, then vanishes. The README/tool-schema claim "persistent cd state" is effectively untrue over HTTP; clients work around it with `cd x && cmd` chains.
- `POST /cancel` runs `cancel_active()` on its own fresh thread → `active_pid` is `None` → **`/cancel` always returns `{"cancelled": false}` over HTTP.** Same for `/env`'s `active_command_pid` (always null). WS is unaffected (per-connection state + frame-loop cancel).
- WS `cd`/cancel work correctly because state lives in a `conn` dict threaded through the executor.

MCP must adopt the WS model (§4.3) — which also makes MCP strictly better than REST for `cd` persistence.

### 2.4 Tool catalog

- ~110 routed endpoints vs 61 `OPENAI_TOOLS` (curated subset, OpenAI `function` wrapper: `{type:"function", function:{name, description, parameters:{type:object, properties, required}}}`). `function.parameters` is already valid JSON Schema (minus `additionalProperties`).
- Duplicates and naming collisions exist between REST-era names and WS-era names (see A3). The `build_catalog()` helper flattens to `{name, desc, params, category}` — evidence the author already cares about prompt/token economy.

### 2.5 Compatibility facts (checked against the live spec, 2025-06-18)

- stdio transport: messages are **newline-delimited JSON-RPC** on stdin/stdout; stderr for logs; nothing but MCP messages on stdout.
- Streamable HTTP: **one endpoint** (e.g. `/mcp`) serving POST (client→server; may reply `application/json` or open an SSE stream via `Accept: text/event-stream`) and GET (server→client SSE stream). `Mcp-Session-Id` header issued at initialize; `MCP-Protocol-Version` header honored (absent ⇒ assume 2025-03-26). Origin validation is a spec **MUST** (DNS rebinding); loopback binding + auth recommended.
- Lifecycle: `initialize` (version negotiation, capabilities) → client `notifications/initialized` → `tools/list` / `tools/call`; JSON-RPC errors −32700/−32600/−32601/−32602/−32603; `ping`; tool execution failures are `isError: true` results, not JSON-RPC errors.
- RikkaHub (the client named in the issue): network transports only (SSE + Streamable HTTP), Bearer via `headers: [["Authorization","Bearer …"]]`, per-tool approval toggles. **Stdio is irrelevant for RikkaHub** — HTTP transport is the primary deliverable; stdio is a bonus for desktop clients via SSH/ADB port-forward.

---

## 3. Design

### 3.1 Principles

1. **Zero-touch REST**: existing modules are read-only inputs. All new behavior lives in new files. Any edit to existing files must be behavior-neutral when MCP is disabled (verified by running the old suite + manual smoke tests).
2. **Dispatch to the safe layer**: every MCP tool call that has a REST handler runs that *handler* (risk gates, snapshots, trash, auto-yes, caps for free). No third copy of command builders.
3. **WS-style session state**: each MCP session = `{cwd, active_pid, killed, lock}` like `websocket.py`'s `conn`, which fixes A2 for MCP.
4. **Stdlib-only** (project has zero runtime deps; pip on-device is a real cost). Hand-rolled JSON-RPC mirrors the existing hand-rolled WebSocket — ~350 lines for transport. (Official `mcp` SDK remains the fallback if spec surface grows; see §7.)
5. **Opt-in**: MCP disabled by default ⇒ live behavior bit-identical.

### 3.2 Transports

| Transport | Endpoint/IO | Purpose |
|---|---|---|
| Streamable HTTP (2025-06-18) | `POST/GET http://127.0.0.1:8081/mcp` | **Primary** — RikkaHub & any network MCP client. SSE streams for long `tools/call`. |
| stdio | stdin/stdout, NDJSON | Desktop AI apps that spawn `termux-native-mcp --stdio` via ADB/SSH `-R`. |

- Old 2024-11-05 HTTP+SSE (`/sse` + `/messages`) is **not** needed: RikkaHub supports Streamable HTTP directly (per its docs, streamable is "the newer, recommended transport"); if a legacy client shows up, that transport is ~80 extra lines behind the same dispatcher — defer.
- **Command naming** (settled): the REST server keeps `termux-mcp` exactly as today. The native MCP server is a *separate binary* `termux-native-mcp` (same package, new console-script entry): bare invocation = Streamable HTTP on `127.0.0.1:8081`; `--stdio` = stdio mode; `--port N` overrides the port. REST's `termux-mcp` startup log prints one hint line — `Native MCP server: run 'termux-native-mcp'` (a log string; no behavior change).
- Default MCP port `8081` (env `TERMUX_NATIVE_MCP_PORT`). Host defaults to `127.0.0.1` and non-loopback **requires** auth, mirroring REST's rule in `server.py:33`.

### 3.3 Protocol surface (v1)

- `initialize` → `{protocolVersion: "2025-06-18" (or min(client, latest)), capabilities: {tools: {}, logging?}, serverInfo: {name: "termux-native-mcp", version: 0.9.x}, instructions: brief "runs shell commands on this Android device; files under ~/termuxGPT are safety snapshots/trash"}`
- `notifications/initialized`, `ping`, `tools/list` (cursor optional — return everything in one page ≤ ~64 tools, see §3.5), `tools/call`.
- Server notifications (HTTP mode): `notifications/progress` when the client passed `_meta.progressToken` on a long `tools/call` (spec-supported; cheap since the executor already sees lines).
- v1 deliberately omits: `resources`, `prompts`, `completions`, `logging` (nice-to-have phase 2 — filesystem resources could expose `~/storage` read-only later), `tools/listChanged` (static list).
- Session mgmt (HTTP): `Mcp-Session-Id` (UUID) issued on initialize result; honored/required afterwards (400 without, 404 when expired); `DELETE /mcp` tears the session down (kill running command, drop state); idle sessions expire after a configurable TTL (default 30 min) — **no TTL kill of running commands**, only of idle state.

### 3.4 Security (spec MUSTs + project norms)

- **Origin validation** on all MCP HTTP requests: allowlist env `TERMUX_NATIVE_MCP_ORIGIN` (comma-separated); if unset, loopback-only default behavior + no `Origin` header accepted from non-loopback bindings. Spec's DNS-rebinding MUST.
- **Auth**: reuse the same `TERMUX_MCP_AUTH_TOKEN` scheme as REST (constant-time compare, `handler.py:50`): `Authorization: Bearer` on every MCP HTTP request, plus `?token=` for stdio-less clients is not applicable (stdio inherits the parent process's environment — client must set the env var; document that).
- Non-loopback bind ⇒ auth mandatory (mirror `server.py:33`); MCP **never** binds `0.0.0.0` by default.
- Every shell path still passes `security.py` + `safety.py` because dispatch reuses handlers (§3.6). No bypass parameters accepted; `confirmed` only unlocks *confirmation*-level risks, exactly as REST.

### 3.5 Tool exposure

Single MCP endpoint exposing **one curated tool list** (not all ~110). Source of truth: new `MCP_TOOL_ROUTES` table in the bridge module mapping **unique tool name → REST handler method + input-schema**:

- Take `OPENAI_TOOLS`, drop WS-era duplicates (`camera→camera_photo`, `wifi→wifi_info`, `sms→sms_send`, `tts→tts_speak`, `ocr→text_extract`), drop REST-doc'd endpoints with no schema entry **that matter** are *added* deliberately only if safe & small (e.g. `text_extract`, `db_query`, `translate`, `git_op`, `patch`, `web_server` — explicit decision per tool; default keeps the list ≈ 61).
- Add MCP-native tools that have no REST equivalent but solve MCP's long-run problem:
  - `session_start` / `session_run` / `session_poll` / `session_list` / `session_kill` (tmux-backed, non-blocking — already proven in WS; the recommended way to run `pkg upgrade`-class jobs without holding a `tools/call` open). State is **per-MCP-session**, fixing WS's shared `_ws_session` global.
  - `cancel` (session-scoped pid + kill flag — the one tool MCP implements natively, because REST `/cancel` is broken per A2).
- Filtering env `TERMUX_NATIVE_MCP_TOOLS` = comma list (or `-`-prefixed exclusions) for power users; default = curated list. Profile presets (`core`, `device`, `media`, `smart`, `devops`) map to the existing `build_catalog` categories — future nicety, schema carries a `category` extension only if clients ignore unknown fields (they do per spec `additionalProperties:false` rules — actually **no**: MCP requires strict schema compliance, so keep custom fields out of `inputSchema`; categories live server-side in config only).
- Tool list order: curated manual order (most-used first: `run`, `ls`, `read`, `write`, …), not alphabetical — client context shows the first N.

`tools/call` param mapping: MCP arguments object == REST JSON body (`{cmd}`, `{path, content}`…). Camel/snake already aligned. The `detailed`/`bare` bool quirks are inherited as-is (documented drift between schema and handler for `ls`: schema says `detailed`, handler reads `bare`/`no_dotfiles` — **fix in bridge by normalizing**, i.e. accept `bare`/`no_dotfiles` and map `detailed`→`-la` default behavior; see open questions).

### 3.6 Dispatch: the virtual-handler bridge (zero-duplication reuse)

All REST handlers only use the 5-member HTTP surface + `json_response`. So:

```python
class VirtualHandler:                 # NEW: mcp_bridge.py
    """Presents the BaseHTTPRequestHandler surface; captures everything.
    wfile collects raw bytes (chunked framing included); headers & status
    are recorded, not sent."""
    def send_response(self, status, *a): self.status = status
    def send_header(self, k, v): self.headers_out.append((k, v))
    def end_headers(self): self._chunked = ("Transfer-Encoding","chunked") in pairs
    wfile = CapturingWriter  # .write/.flush append to buffer
```

Dispatch procedure per `tools/call`:

1. Resolve `MCP_TOOL_ROUTES[name]` → handler callable + `handler`-surface mode.
2. Session worker **seeds the thread-local** before invoking: `set_current_dir(session["cwd"])` (fixes A2 — handler code and `execute_streaming` read tld transparently), and reads it back afterwards into `session["cwd"]` so `cd` persists per MCP session. (Per-request threads make this safe; no REST code is affected.)
3. Call `handler(virtual, params_dict)` synchronously.
4. Decode captured response → MCP result:
   - **chunked text** (`Transfer-Encoding: chunked`): de-chunk (strip length frames) → output text. Parse terminal markers → `isError` (`❌ Exit code: N` ≠ 0 or `⏱️ Timed out`), strip the marker line from content, keep truncation marker verbatim.
   - **JSON** (`application/json`): parse body. If `requires_confirmation: true` ⇒ result text = the same message the REST client sees + instruction "re-invoke with `confirmed: true`", `isError: false` (mirrors REST 200). If `blocked`/`error`/status ≥ 400 ⇒ `isError: true` with the error text.
   - Status 400/403 already emitted as JSON by handlers — same rule as above.
5. Wrap result as `{"content": [{"type": "text", "text": …}], "isError": …}`. (Structured JSON passthrough for JSON tools is a v1.1 option — `structuredContent` support is inconsistent across clients; text-first is safest.)

Tools without a REST handler but natively implemented (see §3.5) bypass the virtual handler and call the WS-style executor directly. **`run` is native too** (implemented, not bridged): it needs session `cd` + working `cancel`, and it calls the exact same primitives the REST handler uses (`get_risk_assessment`, `snapshot_targets_from_command`, `preprocess`, auto-yes, output caps), so the gates are identical. Every other tool with a REST handler dispatches through the VirtualHandler (snapshot/trash parity included).

### 3.7 Long-running commands & streaming over MCP

- MCP has no mid-call output streaming — a `tools/call` returns when done. Two sanctioned patterns:
  1. **Progress notifications**: if the client sent `params._meta.progressToken`, the executor emits `notifications/progress` per output line (HTTP: over the GET SSE stream or the POST's SSE response; stdio: as NDJSON lines). Client shows "running…".
  2. **tmux sessions** (`session_run`/`session_poll`, §3.5): fire-and-forget + poll — the pattern WS already shipped (v0.8.3) precisely because HTTP couldn't hold calls open.
- Output cap & no-timeout default carry over unchanged (MCP inherits `MAX_OUTPUT_BYTES`/`TERMUX_MCP_TIMEOUT` semantics via the bridge). Client-side request timeouts are the client's problem; `session_*` + cancel are the answer, and are documented in `instructions`.

### 3.8 Configuration (all new, all opt-in; names follow the binary)

| Env var / flag | Default | Meaning |
|---|---|---|
| `TERMUX_NATIVE_MCP_PORT` | `8081` | MCP Streamable-HTTP port (`0`/unset with the REST daemon = MCP disabled) |
| `--port N` | — | CLI override of the above |
| `TERMUX_NATIVE_MCP_HOST` | `127.0.0.1` | bind; non-loopback ⇒ auth required |
| `TERMUX_NATIVE_MCP_ORIGIN` | (empty = loopback) | allowed `Origin` values, comma-separated |
| `TERMUX_NATIVE_MCP_TOOLS` | (curated list) | include/exclude filters |
| `TERMUX_NATIVE_MCP_SESSION_TTL` | `1800` | idle session expiry seconds |
| `--stdio` | HTTP mode | run stdio mode instead of HTTP |

Auth: `TERMUX_NATIVE_MCP_AUTH_TOKEN` if set, else fall back to the shared `TERMUX_MCP_AUTH_TOKEN` (one secret for both servers). REST vars (`PORT`, `MAX_OUTPUT`, `TIMEOUT`) keep their meaning and are not consulted by the native server.

---

## 4. Implementation plan (file-by-file)

### Phase 0 — nothing to do: baseline

Freeze `main` at v0.9.0; note SHA `dd812fe`. All work on `feature/mcp`.

### Phase 1 — new modules (no edits to live files)

| New file | Contents | Est. size |
|---|---|---|
| `termux_mcp/mcp_config.py` | lazy env reads: `TERMUX_NATIVE_MCP_*` (+ auth fallback to `TERMUX_MCP_AUTH_TOKEN`), protocol constants | ~55 |
| `termux_mcp/mcp_bridge.py` | `VirtualHandler`, `CapturingWriter`, chunk de-framer, route table (name → handler), schema re-shape (`OPENAI_TOOLS` → MCP `tools/list` items), result-decode rules (§3.6), tool filter logic | ~326 |
| `termux_mcp/mcp_core.py` | MCP session type (WS-style `{cwd, pid, killed, lock, closed}`), session registry + TTL, JSON-RPC dispatch, native executors for `run`/`cancel`/`session_*` (same risk/safety primitives as REST), progress-notification hook | ~568 |
| `termux_mcp/mcp_transport_http.py` | Streamable-HTTP endpoint on `ThreadingHTTPServer`: POST handler (JSON-RPC → reply `application/json` or SSE), GET SSE stream per session, `Mcp-Session-Id` issuance/validation, `MCP-Protocol-Version` negotiation, Origin check, Bearer auth, `DELETE`, 404/400 semantics per spec | ~300 |
| `termux_mcp/mcp_transport_stdio.py` | NDJSON read loop on stdin → dispatch → NDJSON on stdout; stderr logging; same session/cancel semantics (single session; cwd persists process-lifetime) | ~120 |
| `termux_mcp/mcp_server.py` | `run_http()`, `run_stdio()` (and optional later `run_dual()`); reuse `server.py` guard logic patterns (auth length check, loopback rule) by **importing** from `.config`/`.security` only | ~80 |
| `termux_mcp/mcp_main.py` | CLI entry: `argparse` for `--stdio` / `--port`, env dispatch → `run_http()` / `run_stdio()`; the `termux-native-mcp` console-script target | ~40 |
| `tests/test_mcp_bridge.py`, `tests/test_mcp_core.py`, `tests/test_mcp_http.py` | stdlib `unittest` — 51 tests: dechunk/marker decoding, registry integrity, schema validation, risk gates (no execution), session TTL, stdio loop, live HTTP transport (ephemeral port) | ~614 |
| `docs/mcp.md` | user-facing: RikkaHub config JSON, curl smoke tests, desktop-client stdio sample, env table | ~120 |

### Phase 2 — additive packaging (the only edits, and they're opt-in)

- `pyproject.toml`: add a second `[project.scripts]` entry — `termux-native-mcp = "termux_mcp.mcp_main:main"` — next to the untouched `termux-mcp = "termux_mcp.__main__:run"`. `pkg install termux-mcp` installs both commands; the REST console script is unchanged and still boots REST only.
- Version bump → 0.10.0 (semver: additive feature).
- README: new "MCP support" section with a "which command do I run?" table (`termux-mcp` = REST daemon · `termux-native-mcp` = native MCP server); **existing** docs untouched.
- Optional later convenience (NOT in v1): an in-process dual daemon (`TERMUX_MCP_NATIVE_PORT`-style guard in `__main__.py`, dead code when unset). v1 ships the separate `termux-native-mcp` process → zero edits to `__main__.py`/`server.py`, strongest isolation.

### Phase 3 — verification (no live impact)

1. **Unit**: bridge decode rules (chunked/JSON/statuses/markers), schema re-shape, filter logic, session TTL/cancel.
2. **Integration on the phone** (or Windows shell with a fake `HOME`): start MCP port with the live REST server running on 8080; run official-spec smoke client:
   - `initialize` → session header; `tools/list` (≤ N tools, all schema-valid); `tools/call run {cmd: "cd /sdcard && pwd"}` then a second call asserting **cwd persisted** (proves MCP fixes A2); `tools/call delete` without `confirmed` (expects confirmation text), with `confirmed` (expects trash path echo); `write` then `ls ~/termuxGPT/snapshots` (snapshot exists); risk-blocked command (`rm -rf ~`) ⇒ `isError` with block message.
3. **RikkaHub on-device**: add server `{"type":"http", url:"http://127.0.0.1:8081/mcp", commonOptions:{headers:[["Authorization","Bearer <token>"]]}}`; exercise a shell tool + a session tool.
4. **Regression**: `curl /ping`, `/run`, `/write`, `/delete` on 8080 before/after MCP enabled — identical.
5. Optionally CI: a workflow job running the unittest suite + a spec smoke script (existing `python-publish.yml` untouched).

### Phase 4 — post-launch (separate issues, not part of #3)

- Unify the three executors (REST `_run_process`, WS `_ws_run_process`, MCP executor) into one `runner` used by all transports — the drift (A1) becomes a single-source fix. Medium risk, big payoff; do on a branch after MCP ships.
- Fix REST `/cancel` + `/env` `active_command_pid` (move REST to per-session state) — MCP's session model is the blueprint.
- WS `delete`/`write` switch to trash/snapshot semantics (safety parity).
- `resources` (read-only `~/storage`, device info) for filesystem-style MCP clients; `prompts`; legacy 2024-11-05 SSE if a client demands it.

---

## 5. Why this can't break the live version — checklist

- [ ] No existing `.py` file is modified in the recommended path (separate-process mode).
- [ ] MCP is off unless `termux-native-mcp` is invoked (REST daemon behavior untouched by its presence).
- [ ] MCP binds its own port; never calls `network.kill_port` on 8080.
- [ ] REST handler code executes only via `VirtualHandler` when MCP is on — no global mutation; thread-local cwd seeded per MCP call is read back before the thread dies (per-request threads make cross-talk impossible).
- [ ] Import-time side effects: handlers/* already imported by `handler.py`; new modules import the same leaf modules only.
- [ ] Auth: same constant-time compare; no new trust boundaries.
- [ ] `tools/call` never accepts commands the REST layer would reject; `confirmed` semantics identical; snapshots/trash untouched.

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Spec drift in hand-rolled JSON-RPC | Conformance tests against the official client SDK (desktop) in CI; only 4 methods in v1; revisit SDK adoption if methods/resources grow (see below) |
| 100+ tool schema bloat in client context | Curated ≈61-list default + filters + `session_*` replacement of long ops; order by usefulness |
| Long `tools/call` hitting client timeouts | `session_*` non-blocking pattern + progress notifications; document in `instructions` |
| HTTP clients holding SSE open forever | Per-request SSE lifecycle per spec (§3.2/5.3 of transport): close after response; session TTL; DELETE |
| Two processes both killing port 8080 (`kill_port`) | MCP never touches REST's port; its own startup kills only its own port (same `kill_port` helper is fine) |
| In-process dual mode thread interactions (tmux globals in `websocket.py`) | Phase-2 default = separate process; if in-process dual mode, MCP keeps **its own** session state, never `websocket._ws_session` |
| AGPL compliance | New files carry the same header as existing; no copyleft issue with the MCP spec itself (open spec) |
| `requires-python >= 3.8` vs `list[str]` annotations (3.9+) | Low severity (Termux ships 3.11/3.12); bump to `>=3.9` in the MCP release changelog |

## 7. Decision points for the maintainer (open questions)

1. **Hand-rolled stdio/HTTP (recommended, zero deps) vs official `mcp` SDK / FastMCP.** Hand-rolled fits this codebase's zero-dependency philosophy and its precedent (hand-rolled WebSocket, ~960 lines). SDK buys spec longevity + OAuth later, costs pip dependency on-device and API churn. **Recommendation: hand-rolled v1; SDK only if resources/prompts/auth explode.**
2. **Binary layout** — **settled: separate `termux-native-mcp`** (own process, strongest isolation, zero live-file edits; bare = HTTP on 8081, `--stdio` for desktop clients). Revisit an in-process dual daemon only if users ask for one-process convenience later.
3. **Tool list**: expose the curated 61 (recommended) or add long-tail REST tools (db-query, translate, git-op, …)? Default list should match what `build_catalog` calls "core" + session tools.
4. **Tool naming**: keep current names verbatim (some are awkward: `camera_photo`, `text_extract` vs `ocr`) — changing breaks REST-schema parity; MCP can ship `aliases` in tool descriptions. Recommendation: keep names, alias descriptions.
5. **`ls` param mismatch**: schema `detailed` vs handler `bare`/`no_dotfiles` — normalize in the bridge (`detailed: true` → `-l`+dotfiles, `bare: true` → `-1`), preserving REST behavior exactly on the REST side.
6. **Progress notifications**: v1 optional if client sends `progressToken`; confirm RikkaHub forwards it (likely not — most clients don't). Default: session tools carry the load.
7. **Version bump**: 0.10.0 + `protocolVersion` 2025-06-18. Confirm Termux Python ≥ 3.9 assumption holds for the package baseline.

## 8. RikkaHub sample config (for the issue reply)

```json
{
  "type": "http",
  "url": "http://127.0.0.1:8081/mcp",
  "commonOptions": {
    "name": "Termux-MCP",
    "enable": true,
    "headers": [["Authorization", "Bearer <TERMUX_MCP_AUTH_TOKEN>"]]
  }
}
```

Desktop MCP client (via ADB/SSH):
```json
{ "mcpServers": { "termux": { "command": "ssh", "args": ["android", "termux-native-mcp", "--stdio"] } } }
```

---

*Audit & design prepared against `dd812fe` (v0.9.0, AGPL-3.0). No live code was modified in preparing this document.*
