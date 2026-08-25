"""The ``aw-windows-pilot`` MCP server, over Streamable HTTP (``POST /mcp``).

Served from this app's own already-authenticated route rather than as a
stdio subprocess: aw-mcp-gateway spawns stdio children inside *its* own
container, which has neither this app's code nor its config and secret
store. Over HTTP the gateway just makes a call. Same mechanism as
aw-app-android-studio and aw-app-google-maps.

There is no single "configured or not" gate: a call can carry enough
per-call overrides (``remote_host_id``, ``python_exe``) to work even when
the app's stored config is incomplete, so :class:`NotConfigured` only fires
at the moment of the real attempt and is reported as a tool error naming
exactly what is missing.
"""
from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool

from . import remote_host, tools

log = logging.getLogger("aw_apps.windows-pilot")

SERVER_NAME = "aw-windows-pilot"
SERVER_VERSION = "1.0.0"

TOOLS_SCHEMA = tools.TOOLS


def _result(req_id, text: str, is_error: bool) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}],
                       "isError": is_error}}


async def handle_request(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    params = request.get("params") or {}
    name = params.get("name", "")
    args = params.get("arguments") or {}

    handler = tools.HANDLERS.get(name)
    if not handler:
        return _result(req_id, f"Unknown tool: {name}", True)

    try:
        # Handlers block on urllib against aw-backend, and a Windows exec
        # round trip can take tens of seconds. On the event loop that would
        # stall every other app route in this process.
        text = await run_in_threadpool(handler, args)
    except remote_host.NotConfigured as exc:
        return _result(req_id, str(exc), True)
    except Exception as exc:  # noqa: BLE001 — last resort, must not 500 the route
        log.exception("windows-pilot MCP tool %s failed", name)
        return _result(req_id, f"{name} failed: {exc}", True)

    return _result(req_id, text, False)
