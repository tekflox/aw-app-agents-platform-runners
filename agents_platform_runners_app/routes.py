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

import os
import shutil
import subprocess

import httpx
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

    @app.post("/register")
    async def register() -> dict:
        """Register this workspace's local CLI runners with
        agents-platform_multitenant (POST /api/runners/register), so the
        platform's Runners registry reflects what's actually installed here.
        Upsert is server-side (workspace, cli) — safe to click repeatedly,
        never creates duplicates."""
        token = cfg.get("agents_platform_token")
        if not token:
            return {
                "error": "agents_platform_token is not configured — set it in this app's "
                "Settings before registering (see aw-app.json config_schema for how to mint one).",
            }
        base = cfg.get("agents_platform_base", "http://127.0.0.1:10014")
        workspace = os.environ.get("AW_WORKSPACE", "aw")
        payload = {
            "workspace": workspace,
            "runners": [
                {"cli": name, "name": name, **_runner_status(name)}
                for name in RUNNERS
            ],
        }
        url = f"{base.rstrip('/')}/api/runners/register"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {token}"},
                )
            resp.raise_for_status()
            return {"registered": resp.json()}
        except httpx.HTTPStatusError as exc:
            return {"error": f"agents-platform responded {exc.response.status_code}: {exc.response.text[:500]}"}
        except Exception as exc:  # noqa: BLE001 — surfaced as-is to the caller
            return {"error": f"could not reach {url}: {exc}"}

    return app
