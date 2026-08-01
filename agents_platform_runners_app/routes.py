"""
agents_platform_runners_app's mode-agnostic FastAPI sub-app (ADR Decision
2/6: docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-
mode.md) — same integrated/standalone dual-mode contract as every other
aw-app-* backend.

This app has no CLI of its own to install — it depends on the
code-agent-clis app (aw-app.json dependencies.apps) for that, which puts
claude/codex/copilot/cursor-agent on /usr/local/bin. Its two own jobs are
(1) contribute an mcp.json (the ported "aw-agents" MCP, see mcp_server.py)
that aw-mcp-gateway discovers, and (2) report whether those runner
binaries are actually present/working (/status) — a quick real signal
that the dependency actually did its job, not just that it's declared.
"""
from __future__ import annotations

import shutil
import subprocess

from fastapi import FastAPI

RUNNERS = ["claude", "codex", "copilot", "cursor-agent"]


def _runner_status(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"installed": False, "path": None, "version": None}
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False,
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    except Exception as exc:  # noqa: BLE001 — surfaced as-is, not a route failure
        version = f"error: {exc}"
    return {"installed": True, "path": path, "version": version}


def build_routes(config: dict | None = None) -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode (plugin.py /
    __main__.py both call it exactly once)."""
    app = FastAPI(title="agents-platform-runners")
    cfg = config or {}

    @app.get("/status")
    async def status() -> dict:
        return {
            "agents_platform_base": cfg.get("agents_platform_base", "http://127.0.0.1:10014"),
            "runners": {name: _runner_status(name) for name in RUNNERS},
        }

    return app
