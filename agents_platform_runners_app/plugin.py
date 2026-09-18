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
   (config, default the agents-platform-multitenant instance) via mcp.json's
   own env block, so no code in mcp_server.py needed changing.
2. Register a tiny /status route (routes.py) that checks whether
   claude/codex/copilot/cursor-agent actually resolve on PATH and reports
   their version — a live signal that the code-agent-clis dependency
   (aw-app.json dependencies.apps, required) actually did its job. This
   app never installs those CLIs itself; depending on code-agent-clis is
   the reused path (Frederico decision 2026-08-01) instead of duplicating
   install logic in a second place (e.g. agents-platform-multitenant's own
   agent-images Dockerfiles, which were deliberately left untouched).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from . import agent_provisioner as agent_provisioner_mod
from . import execute as execute_mod
from . import execute_secret as execute_secret_mod
from . import execution_index as execution_index_mod
from . import identity_token as identity_token_mod
from . import kanban_dispatch as kanban_dispatch_mod
from . import notion_token_sync as notion_token_sync_mod
from . import platform_base as platform_base_mod
from . import platform_settings as platform_settings_mod
from . import routes as routes_mod
from . import runner_registration as runner_registration_mod
from . import shared_redis as shared_redis_mod
from . import skills_sync as skills_sync_mod
from . import warm_pool as warm_pool_mod

log = logging.getLogger("aw_apps.agents_platform_runners")

# Skills-index watchdog cadences (ADR 2026-08-06). The delta task runs
# immediately on boot — with no ack yet that first tick is a full sync — then
# every DELTA_INTERVAL_S only POSTs when the local skill set actually changed.
# The reconcile task ships the complete list unconditionally so the index can't
# silently drift.
DELTA_INTERVAL_S = 180.0
RECONCILE_INTERVAL_S = 360.0

# Kanban Ready-card sweep (2026-08-21). 60s is not a compromise: the Notion
# webhook this replaces was measured at ~50s end to end on a real card, and its
# own code cites up to ~3min worst case — so a 0-60s sweep is at worst the same
# and usually better, for none of the public-endpoint surface a webhook needs.
KANBAN_SWEEP_INTERVAL_S = 60.0

# identity_token refresh cadence (Kanban feature:ap-runners-auto-mint-identity-
# token). 6h is plenty against a 24h-default, half-life refresh policy — see
# identity_token.py's module docstring for the mint+persist design.
IDENTITY_TOKEN_INTERVAL_S = 6.0 * 3600.0

# runner-registration reassert cadence — deliberately its OWN constant, not a
# reuse of IDENTITY_TOKEN_INTERVAL_S (the pre-2026-09-18 shape). The 2026-
# 09-18 aw-claude 401 incident: activate() generated+persisted a fresh
# execute_secret and register_with_platform()'s very next call to hand it to
# agents-platform-multitenant hit a transient failure (now retried on its own
# — see runner_registration.REGISTER_MAX_ATTEMPTS) with nothing to resync the
# two sides for the full 6h this watchdog inherited from the token-refresh
# cadence — every run through this workspace's runner 401'd for over an hour
# before a human force-registered by hand. Unlike identity_token (a 24h JWT
# with plenty of runway), a wrong execute_secret/caller_token here is a hard
# failure on every single dispatch the moment it drifts, so this watchdog's
# OWN interval must be short enough that any future drift (whatever the
# cause) self-heals in minutes, not hours. on_config_saved's immediate
# reassert-on-change (below) is the primary defense; this is the backstop for
# whatever that misses.
RUNNER_REGISTRATION_REASSERT_INTERVAL_S = 120.0

# execute_secret.ensure_configured() retry cadence — closes a gap the
# 2026-09-18 incident fixes above did NOT cover: ensure_configured() itself
# is only ever called once, from activate(), before this constant existed.
# On a BRAND NEW (or freshly recreated) workspace, activate() can run before
# AW_WORKSPACE_API_KEY/AW_WORKSPACE_API_URL are actually readable yet (both
# are minted by aw-workspace core's OWN boot lifespan, in the same process,
# but ordering relative to every installed app's activate() is not something
# this app controls or can assume never races) — ensure_configured() logs a
# warning and gives up silently in that case, and until this watchdog
# existed nothing ever asked again until the NEXT full app restart/upgrade,
# which could be arbitrarily far away for a long-lived workspace. Reasserted
# on the same short cadence as runner registration for the same reason: a
# missing execute_secret is a hard failure (500) on every single /execute
# call, not something to leave to chance.
EXECUTE_SECRET_ENSURE_INTERVAL_S = 120.0

# agents_platform_base resolution (what address this app calls
# agents-platform-multitenant on) lives in platform_base.py now — the old
# fixed bridge-gateway default (http://172.18.0.1:10014) only ever worked
# when AP-MT ran on the same physical host as the workspace, which is false
# for every real BYOD host. See that module's docstring for the resolution
# order and why a schema-default change alone can't fix this (the value is
# persisted into every install's config row).
_workspace_env = platform_base_mod.workspace_env


def build_mcp_servers(config: dict) -> dict:
    """The ``mcpServers`` object this app's own root mcp.json should
    contain — one server, the ported agent_mcp.py, pointed at
    agents_platform_base. This exact file is what aw-mcp-gateway's
    app-scan reads directly (same contract aw-app-mcp-tools' mcp.json
    uses)."""
    config = config or {}
    base = platform_base_mod.resolve(config)
    token = config.get("agents_platform_token") or ""
    return {
        "agents-platform-runners": {
            "enabled": True,
            "type": "stdio",
            "command": "python3",
            "args": ["-m", "agents_platform_runners_app.mcp_server"],
            # agents-platform-multitenant's require_identity() rejects every
            # request without an aw-backend identity JWT (401) — this is
            # that credential (mcp_server.py sends it as Authorization:
            # Bearer on every call). Mint one with aw-backend's
            # create_identity_jwt(); see this app's README for the command.
            # AW_WORKSPACE_* are for the Kanban dispatch tools, which call
            # aw-app-notion over the workspace API. They have to be baked in
            # for the same reason AGENTS_PLATFORM_TOKEN does: the gateway
            # spawns this upstream inside ITS container, so nothing from the
            # workspace server's environment reaches it — and loopback there
            # is the gateway, not the workspace. Empty values are omitted
            # rather than written blank, so a missing one surfaces as the
            # upstream's own "not set" error instead of a 401 nobody can place.
            "env": {k: v for k, v in {
                "AGENTS_BASE": str(base),
                "AGENTS_PLATFORM_TOKEN": str(token),
                "AW_WORKSPACE_API_URL": _workspace_env("AW_WORKSPACE_API_URL"),
                "AW_WORKSPACE_API_KEY": _workspace_env("AW_WORKSPACE_API_KEY"),
            }.items() if v},
            # aw-mcp-gateway spawns stdio upstreams with cwd defaulting to its
            # own BASE_DIR (/app), which doesn't have this app's package on
            # sys.path — explicit cwd is required so `python3 -m
            # agents_platform_runners_app.mcp_server` resolves. $AW_APPS_ROOT
            # is now mounted into the gateway container at this SAME path it
            # has on the host (/opt/aw-workspace/apps/<id>) — no more
            # gateway-specific /workspace/apps translation (see
            # tekflox/aw-mcp-gateway's aw-app.json).
            "cwd": "/opt/aw-workspace/apps/agents-platform-runners",
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
    def __init__(self) -> None:
        # The exact dict object build_routes()'s /execute, /register and
        # /status closures read `cfg` from. Kept as a persistent instance
        # attribute (not a local var) so on_config_saved() below can mutate
        # it IN PLACE — see routes.py's build_routes docstring for why that
        # identity matters: it's what lets a config save (e.g. restoring a
        # secret wiped by an uninstall/reinstall) take effect on the very
        # next HTTP request, with no app or workspace restart needed.
        self._live_config: dict = {}

    def _refresh_derived_config(self, ctx) -> dict:
        """In-process derived state ONLY, safe to run on every worker: mutate
        ``self._live_config`` in place (never rebind — see the docstring on
        the attribute) and re-point ``execution_index_mod`` at it. No disk,
        no network, no podman — that's what makes this callable from
        :meth:`on_config_reloaded`, which core runs on all
        ``AW_WORKSPACE_WORKERS`` workers, not just the one that served the
        config-save POST."""
        config = getattr(ctx, "config", {}) or {}
        self._live_config.clear()
        self._live_config.update(config)
        execution_index_mod.configure(self._live_config)
        return config

    async def on_config_reloaded(self, ctx) -> None:
        """ATTACH half of a config save (``src/apps/base.py``'s ``Plugin``
        contract) — core calls this on EVERY worker, either inline on the
        request worker or via the ``apps:changed`` broadcast on the other
        nine. Before this existed, ``self._live_config`` was only ever
        refreshed in :meth:`activate` and :meth:`on_config_saved` — both of
        which run on a SINGLE worker — so a worker that never served a
        config-save POST (e.g. the Redis-lease leader running the kanban
        sweep watchdog) could hold a stale ``_live_config`` forever. That is
        the exact failure the kanban_sweep_enabled live test hit on
        2026-09-06: the flag flipped on one worker and the leader never saw
        it. This hook is the fix — no more, no less."""
        self._refresh_derived_config(ctx)

    async def activate(self, ctx) -> None:
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            json.load(f)  # validated at install time — just confirms the file is readable here

        config = self._refresh_derived_config(ctx)

        # Attempt one mint+refresh BEFORE mcp.json is written, so a freshly
        # minted token lands in it on this same pass rather than waiting for
        # the watchdog's next tick. Non-fatal: on failure this logs and
        # activation continues on whatever token is already configured — an
        # app that refuses to activate is worse than one running on an old
        # token. See identity_token.py for why this can't just mutate
        # self._live_config and stop there.
        try:
            refreshed = identity_token_mod.refresh(self._live_config)
            if refreshed:
                self._live_config["agents_platform_token"] = refreshed
        except Exception:  # noqa: BLE001 — activation must never be blocked by this
            log.warning("identity_token: refresh failed at activation", exc_info=True)

        # Auto-generate execute_secret if none is configured (Kanban
        # "execute_secret nunca é auto-gerado — runner falha com 500 numa
        # workspace nova") — same "content is seeded once" rule as
        # identity_token.py above, so the register_with_platform() call right
        # below already carries a working shared secret on a brand new
        # workspace instead of sending None and leaving /execute permanently
        # 500ing until a human types one into Settings. Non-fatal: on failure
        # this logs and activation continues with whatever execute_secret (or
        # lack of one) is already configured.
        try:
            generated_secret = execute_secret_mod.ensure_configured(self._live_config)
            if generated_secret:
                self._live_config["execute_secret"] = generated_secret
        except Exception:  # noqa: BLE001 — activation must never be blocked by this
            log.warning("execute_secret: auto-generate failed at activation", exc_info=True)

        # Auto-register this workspace's runners with agents-platform-
        # multitenant right after the token above is confirmed fresh (Kanban
        # feature:ap-runners-auto-register-on-activation — Frederico's
        # original ask, "ele pode automaticamente registrar os runners na
        # instalação tb, dai ele já sobe os runners da workspace"). Same
        # non-fatal shape as the refresh above: the manual POST /register
        # route (routes.py) remains as a retry surface if this fails (token
        # still missing, agents-platform-multitenant unreachable, etc).
        try:
            result = runner_registration_mod.register_with_platform(self._live_config)
            if result.get("error"):
                log.warning("runner registration failed at activation: %s", result["error"])
            else:
                log.info("runner registration: %s", result.get("registered"))
        except Exception:  # noqa: BLE001 — activation must never be blocked by this
            log.warning("runner registration failed at activation", exc_info=True)

        mcp_doc = write_mcp_json(ctx.package_dir, self._live_config)

        ctx.routes.register(routes_mod.build_routes(self._live_config))

        self._register_skills_watchdog(ctx, self._live_config)
        self._register_kanban_sweep_watchdog(ctx, self._live_config)
        self._register_identity_token_watchdog(ctx, self._live_config)
        self._register_runner_registration_watchdog(ctx, self._live_config)
        self._register_execute_secret_watchdog(ctx, self._live_config)

        # Sweep stale isolated run dirs at ACTIVATION, not only where they are
        # created.
        #
        # The reaper first went into _build_kwargs, on the reasoning that
        # sweeping where dirs appear makes it run exactly as often as they do.
        # That reasoning was wrong in the one way that mattered: a host running
        # WARM containers takes dispatch_turn(), which never builds a new
        # isolated dir — so on a warm host the sweep essentially never ran.
        #
        # Measured, not guessed (2026-09-13): after shipping the reaper and
        # updating this app, a real agent run completed and the backlog did not
        # move — 128 dirs, 12.1 GB, and `isolated/`'s own mtime unchanged from
        # the day before, proving nothing was added or removed.
        #
        # Activation is the complement: it happens on every app update and
        # every workspace restart, independent of how runs are dispatched. The
        # two together cover both shapes of host.
        try:
            execute_mod._reap_isolated_dirs_all()
        except Exception:  # noqa: BLE001 — housekeeping never blocks activation
            log.warning("could not sweep isolated run dirs", exc_info=True)

        # Resolve warm mode from persisted config BEFORE anything asks
        # warm_pool.enabled() — config is the source of truth since 0.32.0
        # (default ON), with the RUNNER_WARM_CONTAINER env var left as a
        # per-host escape hatch. See warm_pool.configure()'s docstring for
        # why the env-only gate had to go.
        warm_on = warm_pool_mod.configure(self._live_config)
        log.info("warm containers: %s", "enabled" if warm_on else "disabled")

        # This app (re)starting is one of warm_pool's invalidation triggers
        # (mirrors agents-platform's own boot-time bump_generation — see
        # main.py's rearm sequence) — every warm container labeled before
        # this boot is stale by construction and drains on its next dispatch.
        if warm_on:
            redis_url = shared_redis_mod.resolve(config)
            if redis_url:
                warm_pool_mod.bump_generation(redis_url)

            # ...and since that bump condemns every existing warm container,
            # boot is also the cheapest moment to clear the ones that already
            # died. Backgrounded: a podman socket that is slow (or absent)
            # must never hold up activation.
            threading.Thread(target=execute_mod.reap_dead_warm_containers,
                             name="warm-reap-boot", daemon=True).start()

        log.info(
            "aw-app-agents-platform-runners activated: mcp.json servers=%s, routes mounted",
            list(mcp_doc["mcpServers"]),
        )

    def _register_skills_watchdog(self, ctx, config: dict) -> None:
        """Register the decentralized skills-index sync watchdog (ADR
        2026-08-06). Skipped (with a log) when the app isn't configured to
        reach agents-platform-multitenant, or when the ``watchdog:tasks``
        capability wasn't granted — the app's other jobs still work."""
        base = platform_base_mod.resolve(config)
        token = config.get("agents_platform_token")
        if not token:
            log.info("skills_sync: agents_platform_token not configured — "
                     "skills index watchdog not started")
            return
        if not ctx.has("watchdog:tasks"):
            log.warning("skills_sync: 'watchdog:tasks' capability not granted — "
                        "skills index watchdog not started")
            return

        workspace = os.environ.get("AW_WORKSPACE", "aw")
        client = skills_sync_mod.SkillsSyncClient(base=base, token=token, workspace=workspace)

        async def _delta() -> None:
            result = await asyncio.to_thread(client.sync_incremental)
            log.info("skills_sync delta: %s", result)

        async def _reconcile() -> None:
            try:
                result = await asyncio.to_thread(client.sync_full)
                log.info("skills_sync reconcile: %s", result)
            finally:
                # The Notion-token reconcile rides THIS tick rather than
                # registering a cadence of its own (Kanban
                # architecture:notion-token-per-tenant-ap-mt-step1): 360s is
                # already the interval that bounds how stale AP-MT's derived
                # state may be, and a second watchdog at the same period is
                # two things to reason about instead of one.
                #
                # In `finally` deliberately — a skills-sync failure (AP-MT
                # briefly down, a 409 storm) must not be able to stop a token
                # rotation from propagating. reconcile_once never raises, so
                # it cannot mask the skills exception on its way out.
                outcome = await asyncio.to_thread(
                    notion_token_sync_mod.reconcile_once, self._live_config)
                # Quiet on the steady state ("fingerprints match", every 6
                # minutes, forever); loud on anything that changed or broke.
                if outcome.get("changed") or not outcome.get("reconciled"):
                    log.info("notion token reconcile: %s", outcome)

        ctx.watchdog.register("skills-sync-delta", _delta, DELTA_INTERVAL_S,
                              run_immediately=True)
        ctx.watchdog.register("skills-sync-reconcile", _reconcile, RECONCILE_INTERVAL_S,
                              run_immediately=False)
        log.info("skills_sync: watchdog registered (workspace=%s base=%s)", workspace, base)

    def _register_kanban_sweep_watchdog(self, ctx, config: dict) -> None:
        """Register the Kanban Ready-card sweep — the trigger that replaces the
        monolith's Notion webhook.

        **Off by default** (``kanban_sweep_enabled``, default false). The
        monolith's webhook is still live while this ships, and two dispatchers
        on one board is precisely the state the claim in
        ``kanban_dispatch.claim_card`` exists to survive — but shipping the code
        dark first means the flag flip is the whole cut-over, and the whole
        rollback, with no deploy either way.

        Three things here differ from the skills watchdog above, all deliberate:

        * the task is registered whatever the flag says, and the flag is read
          **inside** each tick off ``self._live_config`` — the dict
          ``on_config_saved`` mutates in place. Gating the *registration*
          instead would make the rollback an app restart; the whole point of
          this flag is that turning it off takes effect on the next tick. The
          interval is a callable for the same reason.
        * the board is addressed over **loopback**, not the published URL. This
          runs inside the workspace server, so the published URL would route out
          to the tunnel edge — which cuts at ~30s, under this module's own
          60s card-read timeout.
        * ``BoardUnavailable`` is caught and logged here rather than left to
          propagate. An auth or reachability failure in a watchdog is otherwise
          a stack trace every 60s that nobody reads and no board ever shows.
        """
        if not ctx.has("watchdog:tasks"):
            log.warning("kanban sweep: 'watchdog:tasks' capability not granted — "
                        "Ready-card watchdog not started")
            return
        base = platform_base_mod.resolve(config)
        token = config.get("agents_platform_token")
        if not token:
            log.warning("kanban sweep: agents_platform_token not configured — "
                        "Ready-card watchdog not started (every dispatch would 401)")
            return
        def _interval() -> float:
            try:
                return float(self._live_config.get("kanban_sweep_interval_s")
                             or KANBAN_SWEEP_INTERVAL_S)
            except (TypeError, ValueError):
                return KANBAN_SWEEP_INTERVAL_S

        board_url = kanban_dispatch_mod.board_base_url(prefer_loopback=True)

        async def _sweep() -> None:
            if not self._live_config.get("kanban_sweep_enabled"):
                return

            import httpx

            board = kanban_dispatch_mod.BoardClient(base_url=board_url)
            platform_headers = {"Authorization": f"Bearer {token}"}
            try:
                async with httpx.AsyncClient(timeout=30, headers=platform_headers) as c:
                    result = await kanban_dispatch_mod.sweep_ready(
                        board, kanban_dispatch_mod.PlatformClient(c, base))
            except kanban_dispatch_mod.BoardUnavailable as exc:
                log.warning("kanban sweep: board unreachable at %s — %s", board_url, exc)
                return
            if result["considered"]:
                log.info("kanban sweep: %s", result)

        ctx.watchdog.register("kanban-ready-sweep", _sweep, _interval,
                              run_immediately=False)
        log.info("kanban sweep: watchdog registered (enabled=%s, every %.0fs, "
                 "board=%s, platform=%s)",
                 bool(config.get("kanban_sweep_enabled")), _interval(), board_url, base)

    def _register_identity_token_watchdog(self, ctx, config: dict) -> None:
        """Register the half-life ``agents_platform_token`` refresh watchdog
        (Kanban ``feature:ap-runners-auto-mint-identity-token``). See
        identity_token.py for the mint+persist design and refresh policy.

        Not gated on a token already being configured — unlike the skills
        and kanban-sweep watchdogs, this one's whole job is to obtain that
        token in the first place when it's missing. Only the capability
        check can skip it: the watchdog facade is lease-leader gated
        (``src/apps/watchdog.py``), so exactly one worker runs it — a bare
        ``threading.Thread`` would run on all ``AW_WORKSPACE_WORKERS``
        workers and race each other writing the config.
        """
        if not ctx.has("watchdog:tasks"):
            log.warning("identity_token: 'watchdog:tasks' capability not granted — "
                        "refresh watchdog not started")
            return

        async def _refresh() -> None:
            try:
                refreshed = await asyncio.to_thread(identity_token_mod.refresh, self._live_config)
            except Exception:  # noqa: BLE001 — a watchdog tick must never raise
                log.warning("identity_token: refresh watchdog tick failed", exc_info=True)
                return
            if refreshed:
                self._live_config["agents_platform_token"] = refreshed
                log.info("identity_token: refreshed agents_platform_token")

        ctx.watchdog.register("identity-token-refresh", _refresh, IDENTITY_TOKEN_INTERVAL_S,
                              run_immediately=False)
        log.info("identity_token: watchdog registered (every %.0fs)", IDENTITY_TOKEN_INTERVAL_S)

    def _register_runner_registration_watchdog(self, ctx, config: dict) -> None:
        """Register the periodic runner-registration reassert watchdog
        (Kanban feature:ap-runners-auto-register-on-activation). Same cadence
        as the identity-token refresh watchdog above — registration is
        upserted server-side by (workspace, cli) (see
        runner_registration.register_with_platform), so reasserting it on
        this schedule costs a network round-trip and nothing else.

        Not gated on a token already being configured, same reasoning as
        :meth:`_register_identity_token_watchdog`: a token minted moments
        earlier in the SAME activate() pass may still be absent (e.g.
        aw-backend was briefly unreachable), and a later config change could
        clear it again — register_with_platform reports that as a
        non-fatal error rather than this watchdog refusing to start.
        """
        if not ctx.has("watchdog:tasks"):
            log.warning("runner_registration: 'watchdog:tasks' capability not granted — "
                        "reassert watchdog not started")
            return

        async def _reassert() -> None:
            try:
                result = await asyncio.to_thread(
                    runner_registration_mod.register_with_platform, self._live_config)
            except Exception:  # noqa: BLE001 — a watchdog tick must never raise
                log.warning("runner_registration: reassert watchdog tick failed", exc_info=True)
                return
            if result.get("error"):
                log.warning("runner_registration: reassert failed: %s", result["error"])
            else:
                log.info("runner_registration: reasserted (%s)", result.get("registered"))

        ctx.watchdog.register("runner-registration-reassert", _reassert,
                              RUNNER_REGISTRATION_REASSERT_INTERVAL_S, run_immediately=False)
        log.info("runner_registration: watchdog registered (every %.0fs)",
                 RUNNER_REGISTRATION_REASSERT_INTERVAL_S)

    def _register_execute_secret_watchdog(self, ctx, config: dict) -> None:
        """Keep retrying execute_secret.ensure_configured() until it succeeds
        — activate() only ever tries this ONCE (see EXECUTE_SECRET_ENSURE_
        INTERVAL_S's docstring for why a single attempt at activation time
        is not enough on a brand new workspace). A tick is a no-op the
        moment execute_secret is already configured (ensure_configured's own
        early return), so this costs nothing once it has succeeded once —
        same "cheap to keep asking" shape as the runner-registration
        reassert watchdog right above.

        Not gated on anything being configured yet, same reasoning as the
        other watchdogs here: the whole point is to keep trying BEFORE
        anything is configured."""
        if not ctx.has("watchdog:tasks"):
            log.warning("execute_secret: 'watchdog:tasks' capability not granted — "
                        "ensure-configured watchdog not started")
            return

        async def _ensure() -> None:
            try:
                generated_secret = await asyncio.to_thread(
                    execute_secret_mod.ensure_configured, self._live_config)
            except Exception:  # noqa: BLE001 — a watchdog tick must never raise
                log.warning("execute_secret: ensure-configured watchdog tick failed", exc_info=True)
                return
            if not generated_secret:
                return
            self._live_config["execute_secret"] = generated_secret
            log.info("execute_secret: generated and persisted execute_secret on a retry tick")
            # Don't wait for the separate runner-registration watchdog's own
            # next tick (up to RUNNER_REGISTRATION_REASSERT_INTERVAL_S away)
            # to tell agents-platform-multitenant about it — same "reassert
            # immediately on change" reasoning as on_config_saved.
            try:
                result = await asyncio.to_thread(
                    runner_registration_mod.register_with_platform, self._live_config)
            except Exception:  # noqa: BLE001 — non-fatal, the reassert watchdog still covers this
                log.warning("runner_registration: reassert-after-ensure failed", exc_info=True)
                return
            if result.get("error"):
                log.warning("runner_registration: reassert-after-ensure failed: %s", result["error"])
            else:
                log.info("runner_registration: reassert-after-ensure succeeded (%s)",
                         result.get("registered"))

        ctx.watchdog.register("execute-secret-ensure-configured", _ensure,
                              EXECUTE_SECRET_ENSURE_INTERVAL_S, run_immediately=False)
        log.info("execute_secret: ensure-configured watchdog registered (every %.0fs)",
                 EXECUTE_SECRET_ENSURE_INTERVAL_S)

    def register_contributed_agents(self, app_id: str, spec: dict) -> dict:
        """Seed one ``contributes.agents`` declaration into Agents Platform.

        This is the provider side of aw-workspace's agent-contribution
        protocol (its ``src/apps/agents.py``): any installed app declares
        the models, agent configs, groups and agents its features need, and
        the workspace hands the whole declaration here on activation — as
        one call, so this side owns the creation ORDER an Agent's slug
        references depend on.

        **Create-if-absent, matched by slug.** An existing object's CONTENT
        is reconciled, not left forever as it was: aw-workspace's own
        ``read_contributed_agent``/``update_contributed_agent`` calls below
        push a corrected field back onto it, but only when that field still
        holds the value the app itself seeded (see aw-workspace's
        ``src/apps/seeded_state.py`` for the hash-based hand-edit check that
        decides this) — a field the user tuned in the UI is never touched.
        See ``agent_provisioner.py`` for why a 409 on the initial create
        counts as already-there rather than an error.

        Reads ``self._live_config``, not a snapshot, so a token pasted into
        the settings panel after this app came up is used by the next
        activation without a workspace restart.

        Called from aw-workspace's synchronous activation path, which
        already guards against exceptions; raising here is safe but
        pointless.
        """
        config = self._live_config or {}
        provisioner = agent_provisioner_mod.AgentProvisioner(
            base=platform_base_mod.resolve(config),
            token=config.get("agents_platform_token") or "",
            # An app declares `mcp_servers: ["aw-gateway"]` and the whole
            # entry — URL included — is resolved from this workspace's own
            # .mcp.json. No override by default.
            #
            # This used to force http://172.18.0.1:9200/mcp, on the premise
            # that a spawned agent container cannot resolve the docker DNS
            # name in .mcp.json. That premise was checked on 2026-08-14 from
            # inside a live agent container and is false: `aw-app-mcp-gateway`
            # resolves there and answers. Meanwhile the substituted IP was
            # not recognised by agents-platform's stale-token repair (which
            # matched a hardcoded hostname list), so every config this
            # provider seeded silently kept a dead token and its agents ran
            # with zero MCP tools. One source of truth is worth more than a
            # second address that has to stay in sync with someone else's
            # allowlist. Still overridable for a deployment that needs it.
            mcp_url_overrides=(
                {"aw-gateway": config["gateway_mcp_url"]}
                if config.get("gateway_mcp_url") else None
            ),
        )
        created = provisioner.seed(app_id, spec)
        if created:
            log.info("seeded agents platform objects from %s: %s", app_id, created)
        return created

    def _reconcile_provisioner(self):
        """Same construction as the seed path, for the two reconcile hooks.

        Built per call off ``self._live_config`` rather than cached, for the
        reason the seed path documents: a token pasted into settings has to
        take effect without a restart.
        """
        config = self._live_config or {}
        return agent_provisioner_mod.AgentProvisioner(
            base=platform_base_mod.resolve(config),
            token=config.get("agents_platform_token") or "",
            mcp_url_overrides=(
                {"aw-gateway": config["gateway_mcp_url"]}
                if config.get("gateway_mcp_url") else None
            ),
        )

    def read_contributed_agent(self, kind: str, slug: str) -> dict | None:
        """One live object, so the workspace can tell seeded from hand-edited.

        Half of the pair that lets an app correct a prompt it shipped wrong.
        The workspace owns the decision of *what* may change; this only
        reports what is live. See aw-workspace ``src/apps/seeded_state.py``.
        """
        return self._reconcile_provisioner().read(kind, slug)

    def update_contributed_agent(self, kind: str, slug: str, changes: dict) -> bool:
        """Apply the workspace's vetted field changes to one seeded object."""
        return self._reconcile_provisioner().update(kind, slug, changes)

    def read_state(self, kind: str, slug: str) -> dict | None:
        """The tenant-shared seeded-state baseline for one object. See
        aw-workspace's ``src/apps/seeded_state.py`` — this is the provider
        half of the namespace it now delegates to the platform instead of a
        per-workspace file for ``"agents"``."""
        return self._reconcile_provisioner().read_state(kind, slug)

    def write_state(self, app_id: str, kind: str, slug: str, app_version: str,
                    fingerprints: dict) -> dict | None:
        """Record this workspace's seeded-state baseline for one object onto
        the platform's tenant-shared table."""
        return self._reconcile_provisioner().write_state(
            app_id, kind, slug, app_version, fingerprints)

    async def on_config_saved(self, ctx) -> None:
        """Regenerate mcp.json from the newly-saved config (agents_platform_base) —
        aw-workspace's save_app_config calls this BEFORE telling the MCP
        Gateway to /reload (contributes.mcp.reload_on_save), so the gateway
        always scans the file this write just produced.

        Also mutates self._live_config IN PLACE (never rebinds it) — that's
        the same dict object build_routes()'s /execute, /register, /status
        closures hold as `cfg`, so e.g. a rotated execute_secret or
        agents_platform_token is honoured on this app's very next HTTP
        request. Before this fix (found live 2026-08-11, after an
        uninstall+reinstall wiped 3 secret config fields), a config save
        only updated the on-disk config — the routes' in-memory `cfg` was
        still the stale snapshot from activate(), so nothing short of a
        full workspace-process restart made a saved secret actually take
        effect.

        Core calls :meth:`on_config_reloaded` right before this, on this same
        worker, so ``self._live_config`` is already fresh by the time this
        runs — the refresh call below is therefore redundant on that path,
        but stays so this method still works standalone (e.g. a test that
        calls it directly, or a duck-typed caller predating the split)."""
        prev_secret = self._live_config.get("execute_secret")
        prev_token = self._live_config.get("agents_platform_token")

        config = self._refresh_derived_config(ctx)
        mcp_doc = write_mcp_json(ctx.package_dir, self._live_config)
        log.info("aw-app-agents-platform-runners config saved: mcp.json servers=%s", list(mcp_doc["mcpServers"]))

        # Reassert registration IMMEDIATELY when either dispatch credential
        # actually changed — the direct fix for the 2026-09-18 aw-claude 401
        # incident, where a credential change (auto-generated execute_secret,
        # or a hand-typed rotation) took effect on THIS app's own /execute
        # check the moment it saved (the in-place _live_config mutation this
        # method's docstring describes above), but agents-platform-
        # multitenant's copy was left to whatever the periodic reassert
        # watchdog's cadence happened to be — 6h at the time of that
        # incident. A config save is exactly the moment a human or
        # execute_secret.ensure_configured() changes one of these two values,
        # so reasserting right here closes the gap at its source instead of
        # relying only on the watchdog backstop (see
        # RUNNER_REGISTRATION_REASSERT_INTERVAL_S). Backgrounded (register_
        # with_platform is a blocking httpx call) and non-fatal — a slow or
        # unreachable agents-platform-multitenant must not hold up or fail
        # the config save itself; the watchdog still covers this attempt.
        new_secret = self._live_config.get("execute_secret")
        new_token = self._live_config.get("agents_platform_token")
        if new_secret != prev_secret or new_token != prev_token:
            log.info("runner_registration: execute_secret/agents_platform_token changed on "
                     "config save — reasserting registration immediately")

            async def _reassert_now() -> None:
                try:
                    result = await asyncio.to_thread(
                        runner_registration_mod.register_with_platform, self._live_config)
                except Exception:  # noqa: BLE001 — a background reassert must never raise
                    log.warning("runner_registration: reassert-on-save failed", exc_info=True)
                    return
                if result.get("error"):
                    log.warning("runner_registration: reassert-on-save failed: %s", result["error"])
                else:
                    log.info("runner_registration: reassert-on-save succeeded (%s)",
                             result.get("registered"))

            asyncio.create_task(_reassert_now())

        # Settings this panel owns but the platform stores — today the
        # OpenAI key the contributed `openai-*` models need. See
        # platform_settings.py for why the push lives on the save and not
        # on activation.
        platform_settings_mod.push_settings(
            base=platform_base_mod.resolve(self._live_config),
            token=self._live_config.get("agents_platform_token") or "",
            config=self._live_config,
        )

        # A save is also how warm mode itself is turned on/off (the
        # warm_container field) — re-resolve before acting on it.
        warm_on = warm_pool_mod.configure(self._live_config)
        log.info("warm containers: %s", "enabled" if warm_on else "disabled")

        # Any settings save could change what a warm container was spawned
        # with (mcp.json, agents_platform_base, ...) — bump generation so
        # every warm container drains+respawns on its next dispatch, same
        # trigger agents-platform's own config-save path fires.
        if warm_on:
            redis_url = shared_redis_mod.resolve(config)
            if redis_url:
                warm_pool_mod.bump_generation(redis_url)

    async def on_workspace_mcp_changed(self, ctx) -> None:
        """SOME OTHER app was installed, updated or uninstalled on this
        workspace, so the MCP tool surface moved (aw-workspace core's
        ``Plugin.on_workspace_mcp_changed``, fired from
        ``Reconciler._trigger_gateway_reload``).

        That surface is exactly what a warm container's CLI process built its
        MCP clients against — once, at process start, with nothing
        re-initialising them for the container's whole 6h life (see
        :func:`warm_pool.reuse_or_drain`). So a warm container spawned before
        the change keeps serving the old tool list until something condemns
        it, which until this hook existed was a human noticing and recycling
        the session by hand.

        Same one-line answer as :meth:`on_config_saved`'s tail, for the same
        reason, and it fits this hook's contract: one Redis write, globally
        effective, idempotent, and non-disruptive by construction — nothing
        is killed synchronously, each condemned container drains and respawns
        on its own NEXT dispatch. ``bump_generation`` never raises and has 3s
        timeouts, which matters because core awaits this on the install
        critical path.

        Deliberately does NOT re-read config or touch disk: core calls this on
        ONE worker only, so anything per-process would be wrong here."""
        if not warm_pool_mod.enabled():
            return
        redis_url = shared_redis_mod.resolve(self._live_config)
        if redis_url:
            warm_pool_mod.bump_generation(redis_url)

    async def deactivate(self) -> None:
        log.info("aw-app-agents-platform-runners deactivated")
