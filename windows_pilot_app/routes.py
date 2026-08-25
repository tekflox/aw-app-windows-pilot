"""This app's backend sub-app, mounted by the runtime at
``/api/apps/windows-pilot`` behind the workspace's IdentityGuard.

The bearer token goes to ``ctx.secrets`` via ``POST /settings``, never
through the generic config path — that credential can run arbitrary
commands on somebody's personal Windows machine, and app config is plain
and cloud-syncable. The other settings (backend URL, workspace slug, host
id, python, browser) are not secrets and go through the ordinary
``POST /api/apps/windows-pilot/config`` route core already provides. This
file adds only what that route cannot: the token, a status readout, a real
connectivity check, provisioning, and the MCP endpoint.
"""
from __future__ import annotations

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from . import mcp_config
from .mcp import host_agent, remote_host, tools

SECRET_KEY = "remote_token"


def build_routes(ctx) -> FastAPI:
    app = FastAPI(title="windows-pilot")

    @app.get("/status")
    async def status() -> dict:
        token = ctx.secrets.read(SECRET_KEY) or ""
        config = tools.current_config()
        return {
            # "logged_in" is what windows/main.json's auth_status widget binds to.
            "logged_in": bool(token),
            "configured": remote_host.configured(),
            "missing": remote_host.missing_settings(),
            "remote_host_id": config.get("remote_host_id") or "",
            "agent_version": host_agent.agent_version(),
            "tools": [t["name"] for t in tools.TOOLS],
            "tool_count": len(tools.TOOLS),
            "mcp_server": mcp_config.SERVER_NAME,
        }

    @app.post("/settings")
    async def save_settings(data: dict = Body(...)) -> dict:
        token = (data.get(SECRET_KEY) or "").strip()
        if not token:
            return JSONResponse({"ok": False, "error": f"{SECRET_KEY} is required"},
                                status_code=400)
        ctx.secrets.write(SECRET_KEY, token)
        # No restart, no gateway reload: remote_host.py resolves the token per
        # call, so the very next tool call already uses it.
        return {"ok": True, "logged_in": True}

    @app.post("/logout")
    async def clear_token() -> dict:
        ctx.secrets.delete(SECRET_KEY)
        return {"ok": True, "logged_in": False}

    @app.post("/test")
    async def test_connection(data: dict = Body(default={})) -> dict:
        """Ask the host who and where it is.

        A saved token is not a working one — wrong workspace slug, revoked
        token, host offline all look identical to "configured" until
        something actually calls out. This also surfaces the session id,
        which is the difference between a pilotable desktop and a process
        that will screenshot black forever.
        """
        host_override = (data.get("remote_host_id") or "").strip() or None
        if not remote_host.configured(host_override):
            return JSONResponse(
                {"ok": False, "error": remote_host.missing_settings(host_override)},
                status_code=400)
        remote_host.forget_home(host_override)
        try:
            text = tools.HANDLERS["win_pilot_status"](
                {"remote_host_id": host_override})
        except Exception as exc:  # noqa: BLE001 — surfaced, not a 500
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "result": text[:2500]}

    @app.post("/provision")
    async def provision(data: dict = Body(default={})) -> dict:
        """Upload the host agent and install its two dependencies.

        Exposed as a route as well as a tool so the Settings window can run
        setup without anyone having to go through an agent session.
        """
        try:
            text = tools.HANDLERS["win_pilot_provision"](data or {})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "result": text[:4000]}

    @app.get("/mcp.json")
    async def mcp_json() -> dict:
        return {"mcpServers": mcp_config.build_mcp_servers()}

    # ------------------------------------------------------------------
    # MCP — Streamable HTTP, auto-discovered by aw-mcp-gateway's app-scan.
    # ------------------------------------------------------------------

    @app.post("/mcp")
    async def mcp_post(data: dict | list = Body(...)):
        from .mcp.http_handler import handle_request

        messages = data if isinstance(data, list) else [data]
        responses = []
        for message in messages:
            reply = await handle_request(message)
            if reply is not None:
                responses.append(reply)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(data, list) else responses[0])

    @app.get("/mcp")
    async def mcp_get():
        return Response(status_code=405)

    return app
