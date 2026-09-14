"""Register this workspace's local CLI runners with agents-platform-
multitenant (``POST /api/runners/register``) — the logic behind both the
manual ``POST /register`` route (routes.py) and the automatic call now made
at ``activate()`` and on a periodic watchdog (Kanban
feature:ap-runners-auto-register-on-activation).

Extracted so neither caller duplicates it. Frederico's original ask
(Telegram, PT-BR): "ele pode automaticamente registrar os runners na
instalação tb, dai ele já sobe os runners da workspace" — the manual route
already worked, nothing called it on its own.

Same 'credentials are re-asserted' shape as identity_token.py: registration
is upserted server-side by (workspace, cli) — see
agents-platform-multitenant/backend/app/api/runners.py's ``register_runners``
— so reasserting it on a cadence costs a network round-trip and nothing
else, never a correctness risk.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import httpx

from . import platform_base as platform_base_mod

RUNNERS = ["claude", "codex", "copilot", "cursor-agent"]
TIMEOUT_S = 20.0


def runner_status(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"installed": False, "path": None, "version": None}
    # cursor-agent writes a fresh debug-session log under
    # /tmp/cursor-agent-logs-<uid> on every invocation, even a bare
    # --version — this can be polled repeatedly, so suppress it via the
    # CLI's own documented env var rather than accumulating logs.
    env = {**os.environ, "CURSOR_AGENT_DISABLE_DEBUG_LOG": "1"} if name == "cursor-agent" else None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False,
            env=env,
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    except Exception as exc:  # noqa: BLE001 — surfaced as-is, not a route failure
        version = f"error: {exc}"
    return {"installed": True, "path": path, "version": version}


def register_with_platform(config: dict) -> dict:
    """Build the payload from this host's runner set and POST it to
    agents-platform-multitenant's ``/api/runners/register``.

    Returns ``{"registered": ...}`` on success or ``{"error": ...}`` on any
    failure — never raises, so a caller (the ``/register`` route, ``activate()``,
    or a watchdog tick) never has to guard it.
    """
    token = (config or {}).get("agents_platform_token")
    if not token:
        return {
            "error": "agents_platform_token is not configured — set it in this app's "
            "Settings before registering (see aw-app.json config_schema for how to mint one).",
        }
    base = platform_base_mod.resolve(config)
    workspace = os.environ.get("AW_WORKSPACE", "aw")
    # This app's OWN reachable base URL (the "Runner" execute endpoint) —
    # the public BYOD tunnel edge (see execute.py's module docstring for
    # why this is the only proven-reachable path from
    # agents-platform-multitenant, a sibling docker container that cannot
    # reach this workspace's nested-podman container directly). Uses the
    # per-app subdomain shape (bare host, no /api/apps/<slug> prefix —
    # RunnerLLM appends /execute itself) rather than the workspace-wide
    # api.<ws> + path-prefixed shape. Override via config if a workspace's
    # public domain differs.
    own_base_url = config.get("own_base_url") or (
        f"https://agents-platform-runners.app.{workspace}.workspace.aw.tekflox.com"
    )
    payload = {
        "workspace": workspace,
        "base_url": own_base_url,
        "runners": [
            {"cli": name, "name": name, **runner_status(name)}
            for name in RUNNERS
        ],
    }
    url = f"{base.rstrip('/')}/api/runners/register"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(
                url, json=payload, headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        return {"registered": resp.json()}
    except httpx.HTTPStatusError as exc:
        return {"error": f"agents-platform responded {exc.response.status_code}: {exc.response.text[:500]}"}
    except Exception as exc:  # noqa: BLE001 — surfaced as-is to the caller
        return {"error": f"could not reach {url}: {exc}"}
