"""Entrypoint referenced by aw-app.json's ``runtime.entrypoint``
("windows_pilot_app.plugin:WindowsPilotPlugin").

No subprocess, no venv, no port: every tool is an HTTPS call to aw-backend's
remote-hosts routes, served in-process — same shape as aw-app-android-studio.

Config and secrets resolve through a callable rather than being read once,
so saving a setting takes effect on the next tool call with no restart.
"""
from __future__ import annotations

import logging
import os

from . import mcp_config, routes as routes_mod
from .mcp import host_agent, remote_host, tools

log = logging.getLogger("aw_apps.windows-pilot")

# Mirrors aw-app.json's config_schema `default` values. Needed because core's
# Tier-1 (inprocess) activation path hands the plugin the raw stored config
# with NO schema-default merge — only the Tier-2 (container) path and the
# settings-UI display route apply `manifest.config_with_defaults()`. A
# freshly installed Tier-1 app therefore sees "" where its manifest declared
# a default, which aw-app-android-studio hit live on 2026-08-18. Until that
# gap closes in core, every Tier-1 app has to re-declare its defaults here.
_SCHEMA_DEFAULTS = {
    "remote_host_id": "c76c606b0a2a5a8b",
    "python_exe": host_agent.DEFAULT_PYTHON,
    "screenshot_dir": host_agent.DEFAULT_SCREENSHOT_DIR,
    "browser": "edge",
    "cdp_port": 9222,
}


class WindowsPilotPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx

        def _resolve_config() -> dict:
            config = getattr(ctx, "config", {}) or {}
            return {
                "remote_backend_url": config.get("remote_backend_url") or "",
                "remote_workspace": config.get("remote_workspace") or "",
                "remote_token": ctx.secrets.read(routes_mod.SECRET_KEY) or "",
                "remote_host_id": config.get("remote_host_id")
                or _SCHEMA_DEFAULTS["remote_host_id"],
                "python_exe": config.get("python_exe") or _SCHEMA_DEFAULTS["python_exe"],
                "screenshot_dir": config.get("screenshot_dir")
                or _SCHEMA_DEFAULTS["screenshot_dir"],
                "browser": config.get("browser") or _SCHEMA_DEFAULTS["browser"],
                "browser_path": config.get("browser_path") or "",
                "browser_user_data_dir": config.get("browser_user_data_dir") or "",
                "cdp_port": config.get("cdp_port") or _SCHEMA_DEFAULTS["cdp_port"],
            }

        remote_host.set_config_resolver(_resolve_config)
        tools.set_config_resolver(_resolve_config)

        ctx.routes.register(routes_mod.build_routes(ctx))

        port = int(os.environ.get("AW_PORT") or 9030)
        # Rebuilt every boot rather than persisted: the entry embeds this
        # process's hostname and API key, both of which change when the
        # workspace container is recreated.
        doc = mcp_config.write_mcp_json(ctx.package_dir, port)

        log.info(
            "aw-app-windows-pilot activated: mcp server=%s, tools=%s, "
            "agent=%s, remote-hosts=%s",
            sorted(doc["mcpServers"]), len(tools.TOOLS), host_agent.agent_version(),
            "configured" if remote_host.configured()
            else f"NOT SET ({remote_host.missing_settings()})",
        )

    async def deactivate(self) -> None:
        log.info("aw-app-windows-pilot deactivated")
