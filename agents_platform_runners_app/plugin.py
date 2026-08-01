"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("agents_platform_runners_app.plugin:AgentsPlatformRunnersAppPlugin").

This app installs no CLI of its own — its two jobs are:

1. Contribute an mcp.json (mcp_server.py — a straight copy of
   agents-platform's mcp_server/agent_mcp.py, "ported" per Frederico's
   2026-08-01 instruction) that aw-mcp-gateway discovers and reloads on
   config save (contributes.mcp.reload_on_save — same pattern
   aw-app-mcp-tools already uses). agent_mcp.py reads its target platform
   URL from $AGENTS_BASE; this app points that at agents_platform_base
   (config, default the agents-platform_multitenant instance) via mcp.json's
   own env block, so no code in mcp_server.py needed changing.
2. Register a tiny /status route (routes.py) reporting the runner-source
   config agents-platform_multitenant's agent-images Dockerfiles were
   updated to pull install scripts from directly (see that repo's
   agent-images/*/Dockerfile — raw.githubusercontent.com/<runner_source_repo>
   /<runner_source_ref>/scripts/install_<cli>.sh, the SAME scripts
   aw-app-code-agent-clis installs into this workspace, single source of
   truth either way).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import routes as routes_mod

log = logging.getLogger("aw_apps.agents_platform_runners")

DEFAULT_AGENTS_PLATFORM_BASE = "http://127.0.0.1:10014"


def build_mcp_servers(config: dict) -> dict:
    """The ``mcpServers`` object this app's own root mcp.json should
    contain — one server, the ported agent_mcp.py, pointed at
    agents_platform_base. This exact file is what aw-mcp-gateway's
    app-scan reads directly (same contract aw-app-mcp-tools' mcp.json
    uses)."""
    config = config or {}
    base = config.get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE
    return {
        "agents-platform-runners": {
            "enabled": True,
            "type": "stdio",
            "command": "python3",
            "args": ["-m", "agents_platform_runners_app.mcp_server"],
            "env": {"AGENTS_BASE": str(base)},
        }
    }


def write_mcp_json(package_dir: str, config: dict) -> dict:
    """Regenerate this app's own root mcp.json from config and write it to
    disk — the file aw-mcp-gateway scans directly."""
    doc = {"mcpServers": build_mcp_servers(config)}
    path = Path(package_dir) / "mcp.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


class AgentsPlatformRunnersAppPlugin:
    async def activate(self, ctx) -> None:
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            json.load(f)  # validated at install time — just confirms the file is readable here

        config = getattr(ctx, "config", {}) or {}
        mcp_doc = write_mcp_json(ctx.package_dir, config)

        ctx.routes.register(routes_mod.build_routes(config))

        log.info(
            "aw-app-agents-platform-runners activated: mcp.json servers=%s, routes mounted",
            list(mcp_doc["mcpServers"]),
        )

    async def on_config_saved(self, ctx) -> None:
        """Regenerate mcp.json from the newly-saved config (agents_platform_base,
        runner_source_repo/ref) — aw-workspace's save_app_config calls this
        BEFORE telling the MCP Gateway to /reload (contributes.mcp.reload_on_save),
        so the gateway always scans the file this write just produced."""
        config = getattr(ctx, "config", {}) or {}
        mcp_doc = write_mcp_json(ctx.package_dir, config)
        log.info("aw-app-agents-platform-runners config saved: mcp.json servers=%s", list(mcp_doc["mcpServers"]))

    async def deactivate(self) -> None:
        log.info("aw-app-agents-platform-runners deactivated")
