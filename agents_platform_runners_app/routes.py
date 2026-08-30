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
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import execute as execute_mod
from . import observability_push as observability_push_mod
from . import shared_redis

RUNNERS = ["claude", "codex", "copilot", "cursor-agent"]


def _runner_status(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"installed": False, "path": None, "version": None}
    # cursor-agent writes a fresh debug-session log under
    # /tmp/cursor-agent-logs-<uid> on every invocation, even a bare
    # --version — this route can be polled repeatedly, so suppress it via
    # the CLI's own documented env var rather than accumulating logs.
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


def build_routes(config: dict | None = None) -> FastAPI:
    """Mode-agnostic factory — call this fresh for each mode (plugin.py /
    __main__.py both call it exactly once).

    ``cfg`` is bound to the SAME dict object every route closure below
    reads from. plugin.py relies on that identity: it hands us its own
    ``self._live_config`` and, on every ``on_config_saved``, mutates that
    dict IN PLACE (clear()+update(), never rebinds it to a new dict) so a
    config save (e.g. rotating ``execute_secret`` after a wiped-secret
    reinstall, found live 2026-08-11) takes effect on the very next
    request — no full workspace-process restart required. Using
    ``config or {}`` here would silently break that identity the moment
    the live config is empty (``{} or {}`` evaluates the right-hand
    literal, a NEW dict) — use an explicit None-check instead."""
    app = FastAPI(title="agents-platform-runners")
    cfg = config if config is not None else {}

    @app.get("/status")
    async def status() -> dict:
        return {
            "agents_platform_base": cfg.get("agents_platform_base", "http://127.0.0.1:10014"),
            "runners": {name: _runner_status(name) for name in RUNNERS},
        }

    @app.post("/register")
    async def register() -> dict:
        """Register this workspace's local CLI runners with
        agents-platform-multitenant (POST /api/runners/register), so the
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
        # This app's OWN reachable base URL (the "Runner" execute endpoint) —
        # the public BYOD tunnel edge (see execute.py's module docstring for
        # why this is the only proven-reachable path from
        # agents-platform-multitenant, a sibling docker container that
        # cannot reach this workspace's nested-podman container directly).
        # Uses the per-app subdomain shape (bare host, no /api/apps/<slug>
        # prefix — RunnerLLM appends /execute itself) rather than the
        # workspace-wide api.<ws> + path-prefixed shape; both hit the same
        # guarded ASGI sub-app (see aw-app-template/external-client/
        # app-api-client.js's header comment for the generic two-hostname
        # pattern every app on this platform gets). Override via config if a
        # workspace's public domain differs.
        own_base_url = cfg.get("own_base_url") or (
            f"https://agents-platform-runners.app.{workspace}.workspace.aw.tekflox.com"
        )
        payload = {
            "workspace": workspace,
            "base_url": own_base_url,
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

    @app.post("/register-observability")
    async def register_observability() -> dict:
        """Push this workspace's resolved Settings > Observability target to
        agents-platform-multitenant right now. Called by aw-workspace core
        (``src/api/observability.py``'s ``put_observability`` handler) right
        after a mode change saves, over loopback — that's the only caller in
        the normal case, so a save reaches AP-MT within the same request
        cycle, no polling delay. Also usable standalone as a manual retry if
        that push failed (e.g. AP-MT was briefly unreachable) while the save
        itself still succeeded. See ``observability_push.push_once`` for the
        actual two-hop logic."""
        import asyncio
        return await asyncio.to_thread(observability_push_mod.push_once, cfg)

    @app.post("/execute")
    async def execute_job(request: Request) -> dict:
        """Spawn a container in THIS workspace's own container engine and
        stream its output back over the shared Redis Stream (see execute.py's
        module docstring for the full design + the reachability/auth
        investigation that shaped it). Two job shapes share this one route,
        auth, and Redis-publish plumbing — the CLI-agent path (agent runs)
        and the raw_command path (agents-platform-multitenant's monitor
        runs, no LLM in the loop — see execute.py's ``_build_raw_kwargs``);
        a raw_command body starts `bash -lc "<command>"` instead of a CLI,
        which is why this is a mode flag here rather than a second endpoint —
        it needs nothing this route, its auth, or its dispatch/dedup
        machinery don't already do for the CLI path.

        Auth: this app is registered as a PUBLIC app in aw-backend's registry
        (AppInstall.config.public=true) so the tunnel edge's usual aw_id_jwt
        + workspace-membership check is skipped for it — aw-workspace's own
        per-app IdentityGuard still requires a validly-SIGNED identity JWT
        (Authorization: Bearer), and this route additionally requires the
        shared X-Runner-Secret header to match config["execute_secret"].
        Both gates must pass; neither alone is sufficient.
        """
        secret = cfg.get("execute_secret")
        if not secret:
            raise HTTPException(500, "execute_secret is not configured on this app's Settings")
        presented = request.headers.get("x-runner-secret", "")
        if presented != secret:
            raise HTTPException(401, "invalid or missing X-Runner-Secret")

        redis_url = shared_redis.resolve(cfg)
        if not redis_url:
            raise HTTPException(
                500, "shared_redis_url is not configured on this app's Settings and "
                     "could not be derived from this container's default route")

        if not execute_mod.CONTAINER_SOCKET:
            raise HTTPException(
                503, "AW_CONTAINER_SOCKET is not set — this workspace has no container "
                     "engine available to spawn agent CLIs (containers:manage capability "
                     "unmet at runtime, even though granted in aw-app.json)")

        body = await request.json()
        run_id = body.get("run_id") or uuid.uuid4().hex
        # TEMP DEBUG (2026-08-08, remove once confirmed): checking whether
        # agents-platform-multitenant's agent_id-in-payload change
        # (runner.py) has actually been deployed yet. Written to a file
        # under the shared workspace mount (not just logged) because this
        # in-process app's own stdout isn't exposed as a named component in
        # `aw-workspace-cli logs` — the file IS reachable from any other
        # container sharing this workspace's filesystem mount.
        try:
            import time as _time
            _dbg_path = os.path.join(execute_mod.WORKSPACE_CONTAINER_DIR, ".tmp", "execute_debug.log")
            os.makedirs(os.path.dirname(_dbg_path), exist_ok=True)
            with open(_dbg_path, "a") as _dbg_f:
                _dbg_f.write(f"{_time.time():.0f} run_id={run_id} agent_id={body.get('agent_id')!r} "
                             f"session_id={body.get('session_id')!r}\n")
        except Exception:
            pass
        job = {
            "run_id": run_id,
            # Monitor-run path (agents-platform-multitenant's monitor_run.py):
            # a raw shell command, no CLI/session/MCP involved — everything
            # below this stays None/default and _build_container_kwargs
            # branches off to _build_raw_kwargs before touching any of it.
            "raw_command": body.get("raw_command"),
            "cwd": body.get("cwd"),
            "timeout_seconds": body.get("timeout_seconds"),
            "cli": body.get("cli", "claude"),
            "model": body.get("model"),
            "prompt": body.get("prompt", ""),
            "session_id": body.get("session_id"),
            # Whether that id names a conversation that does not exist yet, so
            # the CLI is told to CREATE it (`--session-id`) instead of
            # `--resume`, which on an unknown id returns an empty reply and
            # still exits 0. Only agents-platform can know this — it owns the
            # Run history the answer comes from.
            "new_session": bool(body.get("new_session")),
            # Only used by the RUNNER_WARM_CONTAINER=1 opt-in path (see
            # warm_pool.py) — a warm container's stable name is keyed on
            # BOTH agent_id and session_id, mirroring agents-platform's own
            # warm_pool.py design. Absent -> that path is skipped entirely.
            "agent_id": body.get("agent_id"),
            "allowed_tools": body.get("allowed_tools"),
            "disallowed_tools": body.get("disallowed_tools"),
            "append_system_prompt": body.get("append_system_prompt"),
            "extra_args": body.get("extra_args"),
            "notion_task_id": body.get("notion_task_id"),
            "source_device": body.get("source_device"),
            "mcp_servers": body.get("mcp_servers"),
            "dangerous_skip_permissions": body.get("dangerous_skip_permissions", True),
            "permissions": body.get("permissions"),
            # Files the user attached in the originating chat, carried inline
            # (base64) so they can be written to the agent's own disk and the
            # prompt's URLs swapped for real paths — see
            # aw_attach.materialise_inbound. Absent from older callers, which
            # simply keep getting URL-only prompts.
            "attachments": body.get("attachments"),
            # recycle_session, already resolved by agents-platform from this
            # session's queued level into "drain"/"force" (see that repo's
            # executor.py). Only the warm path can honour it — the container
            # it recycles exists solely on this side. Absent on every
            # ordinary turn, which is what keeps the warm pool warm.
            "warm_recycle": body.get("warm_recycle"),
        }
        # "duplicate" = this run_id was already dispatched by this process, so
        # nothing new was spawned. A retried handshake (RunnerLLM._dispatch
        # retries when the POST fails) must be a no-op, not a second agent on
        # the same run — see start_job's _STARTED_RUN_IDS. Either way the
        # caller's next step is identical: attach to run:{run_id}:events.
        started = execute_mod.start_job(job, redis_url)
        return {"run_id": run_id, "status": "started" if started else "duplicate"}

    return app
