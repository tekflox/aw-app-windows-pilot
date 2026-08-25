"""Entry describing this app's own ``/mcp`` endpoint, for aw-mcp-gateway's
app-scan (``scan_app_mcp_servers()``, which reads ``<app dir>/mcp.json``).

Tier-1 (in-process): this *is* the aw-workspace process, so
``socket.gethostname()`` is exactly the value ContainerSupervisor injects
into sibling containers as ``AW_WORKSPACE_HOST``, and
``AW_WORKSPACE_API_KEY`` is already in this process's environment. The
header is required because Tier-1 routes sit behind IdentityGuard.
"""
from __future__ import annotations

import os
import socket

MCP_SERVER_NAME = "aw-windows-pilot"
ROUTE_PATH = "/api/apps/windows-pilot/mcp"


def build_self_entry(port: int | None = None) -> dict:
    host = socket.gethostname()
    port = port or int(os.environ.get("AW_PORT") or 9030)
    entry: dict = {
        "type": "http",
        "url": f"http://{host}:{port}{ROUTE_PATH}",
        "enabled": True,
    }
    api_key = os.environ.get("AW_WORKSPACE_API_KEY")
    if api_key:
        entry["headers"] = {"X-Api-Key": api_key}
    return entry
