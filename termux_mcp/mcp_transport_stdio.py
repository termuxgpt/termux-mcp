import json
import logging
import sys


from . import mcp_core as core


logger = logging.getLogger(__name__)


SESSION_ID = "stdio"


def _result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _send(out, obj: dict) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    out.flush()


def serve_stdio(inp=None, out=None) -> None:
    inp = inp or sys.stdin
    out = out or sys.stdout
    session = None

    print("[termux-native-mcp] stdio transport ready — "
          "speaking MCP (newline-delimited JSON-RPC)", file=sys.stderr)
    sys.stderr.flush()

    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            _send(out, _error(None, core.PARSE_ERROR, "Parse error"))
            continue
        if not isinstance(msg, dict):
            _send(out, _error(None, core.INVALID_REQUEST,
                              "Message must be an object"))
            continue

        if session is None:
            session = core.create_session("stdio")
            session.sid = SESSION_ID

        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")
        is_request = "id" in msg

        if not isinstance(method, str) or not method:
            if is_request:
                _send(out, _error(req_id, core.INVALID_REQUEST,
                                  "Missing method"))
            continue

        if method == "initialize":
            try:
                res = core.dispatch(session, method, params)
            except core.RpcError as e:
                res = None
                _send(out, _error(req_id, e.code, str(e)))
            if res is not None and is_request:
                _send(out, _result(req_id, res))
            continue

        if not is_request:
            try:
                core.dispatch(session, method, params)
            except core.RpcError as e:
                logger.warning("notification failed: %s", e)
            continue

        try:
            result = core.dispatch(session, method, params)
        except core.RpcError as e:
            _send(out, _error(req_id, e.code, str(e)))
            continue
        except Exception as e:
            logger.exception("dispatch failed")
            _send(out, _error(req_id, core.INTERNAL_ERROR,
                              f"Internal error: {e}"))
            continue
        _send(out, _result(req_id, result if result is not None else {}))

    if session is not None:
        session.killed.set()
        session.kill_active()
