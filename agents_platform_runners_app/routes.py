"""
agents_platform_runners_app's mode-agnostic FastAPI sub-app (ADR Decision
2/6: docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-
mode.md) — same integrated/standalone dual-mode contract as every other
aw-app-* backend.

This app has no CLI of its own to install (unlike aw-app-code-agent-clis)
— its two jobs are (1) contribute an mcp.json (the ported "aw-agents" MCP,
see mcp_server.py) that aw-mcp-gateway discovers, and (2) be a
discoverable, documented home for the "runners" integration with
agents-platform_multitenant. `runner_source_repo`/`runner_source_ref`
below are what agents-platform_multitenant's agent-images Dockerfiles
were updated to pull install scripts from directly (raw.githubusercontent.com
— see that repo's agent-images/*/Dockerfile) rather than duplicating
install logic — this app's /status route just reports that config back so
it's inspectable, not a proxy for it (routes:local, needed for an
unauthenticated external caller like a Docker build to hit this app's own
HTTP endpoint, doesn't exist in the aw-workspace framework yet).
"""
from __future__ import annotations

from fastapi import FastAPI


def build_routes(config: dict | None = None) -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode (plugin.py /
    __main__.py both call it exactly once)."""
    app = FastAPI(title="agents-platform-runners")
    cfg = config or {}

    @app.get("/status")
    async def status() -> dict:
        return {
            "runner_source_repo": cfg.get("runner_source_repo", "tekflox/aw-app-code-agent-clis"),
            "runner_source_ref": cfg.get("runner_source_ref", "v0.2.1"),
            "runners": ["claude", "codex", "copilot", "cursor-agent"],
            "agents_platform_base": cfg.get("agents_platform_base", "http://127.0.0.1:10014"),
        }

    return app
