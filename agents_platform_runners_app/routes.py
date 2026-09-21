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

import logging
import os
import uuid

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile

from . import execute as execute_mod
from . import execution_index as execution_index_mod
from . import notion_token_sync as notion_token_sync_mod
from . import observability_push as observability_push_mod
from . import platform_base as platform_base_mod
from . import runner_registration as runner_registration_mod
from . import shared_redis
from . import warm_pool

# Re-exported for callers/tests that import RUNNERS from this module —
# runner_registration.py is the single source of truth (also used by
# activate()'s automatic registration and its watchdog).
RUNNERS = runner_registration_mod.RUNNERS
_runner_status = runner_registration_mod.runner_status

log = logging.getLogger("aw_apps.agents_platform_runners.routes")


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
    execution_index_mod.configure(cfg)

    def require_execute_secret(request: Request) -> None:
        """The shared-secret half of this app's two-layer auth, shared by every
        route agents-platform-multitenant calls (/execute, /abort).

        See execute_job's docstring for the full picture: aw-workspace's own
        per-app IdentityGuard already demands a validly-signed identity JWT in
        Authorization: Bearer, and this adds the X-Runner-Secret both sides
        configure. Both gates must pass; neither alone is sufficient. As a
        DEPENDENCY it runs before the handler body, so an unauthenticated
        caller still learns nothing about what bodies the route would accept —
        an ordering test_execute_payload_validation.py asserts.

        Deliberately not gated on the app-level public/auth_required flag:
        that is all-or-nothing per app, so /abort inherits /execute's
        IdentityGuard treatment for free and must not change it.
        """
        secret = cfg.get("execute_secret")
        if not secret:
            raise HTTPException(500, "execute_secret is not configured on this app's Settings")
        presented = request.headers.get("x-runner-secret", "")
        if presented != secret:
            raise HTTPException(401, "invalid or missing X-Runner-Secret")

    @app.get("/status")
    async def status() -> dict:
        return {
            "agents_platform_base": platform_base_mod.resolve(cfg),
            "runners": {name: _runner_status(name) for name in RUNNERS},
        }

    @app.get("/warm-containers")
    async def warm_containers(include_draining: bool = False) -> dict:
        """Inventory of warm containers alive on THIS workspace's own
        container engine right now, and whose each one is — see
        warm_pool.list_containers for the label-vs-inferred cli logic.
        `warm_enabled` is returned alongside the list because "warm mode is
        off" and "no warm containers right now" are different answers a
        caller cannot otherwise tell apart from an empty list. A listing
        failure raises rather than returning an empty list, for the same
        reason."""
        if not execute_mod.CONTAINER_SOCKET:
            raise HTTPException(
                503, "AW_CONTAINER_SOCKET is not set — this workspace has no container "
                     "engine available to spawn agent CLIs (containers:manage capability "
                     "unmet at runtime, even though granted in aw-app.json)")
        import docker as docker_sdk
        try:
            client = docker_sdk.DockerClient(base_url="unix://" + execute_mod.CONTAINER_SOCKET)
            containers = warm_pool.list_containers(client, include_draining=include_draining)
        except Exception as exc:  # noqa: BLE001 — surfaced as an error, never as []
            raise HTTPException(502, f"could not list warm containers: {exc}") from exc
        return {"warm_enabled": warm_pool.enabled(), "containers": containers}

    @app.post("/register")
    async def register() -> dict:
        """Register this workspace's local CLI runners with
        agents-platform-multitenant (POST /api/runners/register), so the
        platform's Runners registry reflects what's actually installed here.
        Upsert is server-side (workspace, cli) — safe to click repeatedly,
        never creates duplicates.

        Same logic activate() and its periodic watchdog call automatically
        (Kanban feature:ap-runners-auto-register-on-activation) — this route
        stays as a manual trigger/retry surface. See
        runner_registration.register_with_platform for the shared logic."""
        import asyncio
        return await asyncio.to_thread(runner_registration_mod.register_with_platform, cfg)

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

    # ------------------------------------------------------------------
    # Notion token relay — see notion_token_sync.py for why this app is the
    # one making the AP-MT call for a token it does not own. Callers are
    # aw-app-notion over loopback (workspace X-Api-Key); nothing here reads
    # or returns a token, only stores/clears one and reports a fingerprint.
    # ------------------------------------------------------------------

    def _notion_token_failure(exc: Exception) -> HTTPException:
        """409 when this workspace has no agents-platform at all, 502 when it
        has one that failed. aw-app-notion's logout treats those differently —
        "there is no remote copy" must let a logout through, "I could not
        delete the remote copy" must stop it — so the distinction has to
        survive the hop as a status code, not just prose."""
        if isinstance(exc, notion_token_sync_mod.NotionTokenNotConfigured):
            return HTTPException(409, str(exc))
        return HTTPException(502, str(exc))

    @app.post("/notion-token")
    async def notion_token_push(data: dict = Body(...)) -> dict:
        import asyncio

        token = (data.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "token is required")
        try:
            return await asyncio.to_thread(notion_token_sync_mod.push, cfg, token)
        except notion_token_sync_mod.NotionTokenSyncError as exc:
            raise _notion_token_failure(exc) from exc

    @app.delete("/notion-token")
    async def notion_token_delete() -> dict:
        import asyncio

        try:
            return await asyncio.to_thread(notion_token_sync_mod.delete, cfg)
        except notion_token_sync_mod.NotionTokenSyncError as exc:
            raise _notion_token_failure(exc) from exc

    @app.get("/notion-token/state")
    async def notion_token_state() -> dict:
        import asyncio

        try:
            return await asyncio.to_thread(notion_token_sync_mod.state, cfg)
        except notion_token_sync_mod.NotionTokenSyncError as exc:
            raise _notion_token_failure(exc) from exc

    # ------------------------------------------------------------------
    # AP-MT proxies for apps that have no AP-MT credential of their own.
    #
    # aw-app-crispal used to hold three config fields (ap_gallery_base,
    # ap_token, ap_inject_secret) to reach agents-platform-multitenant
    # directly. None of them were ever declared in its config_schema, so they
    # resolved empty and every one of those calls was dead — the Arvin
    # archive, the Arvin wake-up, and the gallery tools alike. Rather than
    # declare a second copy of a credential THIS app already mints and rotates
    # (identity_token.py), the credential stays here and Crispal reaches AP-MT
    # through these three thin proxies, over the workspace API key it already
    # has (its `_workspace_api()`). See .tmp/gallery-migration/PLAN.md §2.
    #
    # Deliberately thin: no reshaping of request or response, and AP-MT's own
    # status code is what the caller sees. A proxy that invents its own error
    # vocabulary is a second place to debug.
    # ------------------------------------------------------------------

    def _ap_target() -> tuple[str, dict[str, str]]:
        """(base_url, auth headers) for an AP-MT call made on this app's own
        identity. A missing token is 503, not 401: the caller presented
        perfectly good credentials to US — it is this app that has nothing to
        present onward, and the fix is on this side (auto-mint hasn't run, or
        aw-backend refused it)."""
        token = str(cfg.get("agents_platform_token") or "").strip()
        if not token:
            raise HTTPException(
                503, "this app holds no agents_platform_token — agents-platform "
                     "cannot be called on its behalf (see identity_token.py's "
                     "auto-mint, and this app's Settings)")
        return platform_base_mod.resolve(cfg).rstrip("/"), {"Authorization": f"Bearer {token}"}

    def _ap_answer(resp: httpx.Response, what: str) -> dict:
        """AP-MT's body on success, AP-MT's status + body as an HTTPException
        otherwise. Never swallows: the caller of /gallery/upload is a queue
        worker that has to be able to tell "the gallery refused this" from
        "the upload worked" — conflating them once already marked successful
        Arvin jobs as crashed and replayed a 5-minute cycle."""
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"{what}: {resp.text[:500]}")
        return resp.json()

    def _ap_unreachable(what: str, exc: Exception) -> HTTPException:
        return HTTPException(502, f"{what}: could not reach agents-platform — {exc}")

    @app.post("/gallery/upload")
    async def gallery_upload(bot_slug: str = Form(...), source: str = Form("agent"),
                             files: list[UploadFile] = File(...)) -> dict:
        """Proxy for AP-MT's `POST /api/admin/gallery/upload` — file generated
        images into the gallery so the GALLERY owns the bytes, rather than a
        row pointing at a disk only the producer can read."""
        base, headers = _ap_target()
        parts = [("files", (f.filename or "image", await f.read(),
                            f.content_type or "application/octet-stream"))
                 for f in files]
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{base}/api/admin/gallery/upload",
                                         data={"bot_slug": bot_slug, "source": source},
                                         files=parts, headers=headers)
        except httpx.HTTPError as exc:
            raise _ap_unreachable("gallery upload", exc) from exc
        return _ap_answer(resp, "gallery upload")

    @app.get("/runs/{run_id}/initiator")
    async def run_initiator(run_id: str) -> dict:
        """Proxy for AP-MT's `GET /api/telegram/run-initiator/{run_id}` — the
        (initiator_kind, initiator_id) an unattended job needs to know which
        chat to wake when it finishes. Not `/api/runs/{id}`: that one is
        behind a person's identity gate, which is what 401'd in silence for
        13 finished Arvin cycles."""
        base, headers = _ap_target()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{base}/api/telegram/run-initiator/{run_id}",
                                        headers=headers)
        except httpx.HTTPError as exc:
            raise _ap_unreachable("run-initiator lookup", exc) from exc
        return _ap_answer(resp, "run-initiator lookup")

    @app.post("/telegram/inject")
    async def telegram_inject(body: dict = Body(...)) -> dict:
        """Proxy for AP-MT's `POST /api/telegram/inject` — put a synthetic
        message into an existing (bot, chat) session, the mechanism a
        background job uses to tell the agent that asked for it that the work
        is done. The body passes through untouched; AP-MT owns its schema.

        This also retires the recurring `AGENTS_TELEGRAM_INJECT_SECRET not
        found at /app/repos/agents-platform/.env` failure (2026-08-13 onward):
        /inject accepts the same `require_tenant_or_service` identity this app
        already holds, so no shared inject secret needs to exist anywhere."""
        base, headers = _ap_target()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{base}/api/telegram/inject",
                                         json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise _ap_unreachable("telegram inject", exc) from exc
        return _ap_answer(resp, "telegram inject")

    @app.post("/execute", dependencies=[Depends(require_execute_secret)])
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
        Both gates must pass; neither alone is sufficient — enforced by the
        shared `require_execute_secret` dependency above, which /abort uses too.
        """
        # Validate the job BEFORE spending a container on it. Until 2026-08-30
        # this route accepted ANY body: `{}` passed straight through to
        # start_job, which spawned a real claude container on an empty prompt
        # and billed a full cold start to produce nothing. Found live while
        # probing whether /execute was reachable at all — the probe itself
        # started run f7122833. A caller that cannot name the work has a bug;
        # answering 400 tells it so, where 200 taught it the opposite.
        #
        # The two job shapes are mutually exclusive by construction:
        # _build_container_kwargs branches on raw_command before it reads
        # prompt (execute.py:481), so a body carrying both is ambiguous — the
        # prompt would be silently discarded. Reject rather than pick.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "body is not valid JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        raw_command = body.get("raw_command")
        prompt = body.get("prompt")
        has_raw = isinstance(raw_command, str) and raw_command.strip() != ""
        has_prompt = isinstance(prompt, str) and prompt.strip() != ""
        if has_raw and has_prompt:
            raise HTTPException(
                400, "body carries both 'raw_command' and 'prompt' — these select "
                     "different job shapes and only raw_command would be honoured; "
                     "send exactly one")
        if not has_raw and not has_prompt:
            raise HTTPException(
                400, "body must carry a non-empty 'prompt' (CLI-agent job) or a "
                     "non-empty 'raw_command' (monitor run) — nothing to execute")

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
            # This turn is a bare CLI slash command ("/compact") and must reach
            # the container at position 0 — the warm path's own per-turn
            # context header would displace it. See
            # warm_pool._with_claude_turn_context. Absent on an older
            # agents-platform's body, which reads as False: the pre-2026-09-11
            # behaviour, i.e. the bug this exists to fix, not a new one.
            "raw_prompt": body.get("raw_prompt"),
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

    @app.post("/abort", dependencies=[Depends(require_execute_secret)])
    async def abort_run(data: dict = Body(...)) -> dict:
        """Kill the container running ``run_id`` on this workspace.

        /execute's counterpart, and the whole point of this endpoint:
        agents-platform-multitenant's own ``kill_run`` can only reach ITS
        host's docker daemon, which for a Runner-backed run holds no container
        at all — so a Telegram ``/abort`` marked the Run row cancelled while
        the agent kept running here to completion (Kanban
        bug:abort-does-not-propagate-to-runner-backed-run).

        Always answers 200 when the request itself was well-formed, with
        ``status`` saying whether anything was killed. An abort that raced a
        run to its finish is a clean ``not_found``, NOT a 404: the caller
        retries 404 as a transient app-reload/tunnel failure (RunnerLLM's
        RETRYABLE_STATUS), so answering 404 here would make it hammer an
        abort three times over three seconds for a run that was simply done.

        ``agent_id``/``session_id`` are optional and only help the warm path,
        whose container name is keyed on that pair rather than on the run id.
        """
        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            raise HTTPException(400, "run_id is required")

        # Degraded but still useful without Redis: this worker's own registry
        # and the deterministic container names still resolve. Not a 500 —
        # abort's whole job is to stop something that is costing money.
        redis_url = shared_redis.resolve(cfg)
        if not redis_url:
            log.warning("abort: run=%s — no shared Redis resolvable; falling back to "
                        "this worker's registry and the deterministic names", run_id)

        import asyncio
        return await asyncio.to_thread(
            execute_mod.abort_job, run_id, redis_url,
            agent_id=data.get("agent_id"), session_id=data.get("session_id"),
        )

    return app
