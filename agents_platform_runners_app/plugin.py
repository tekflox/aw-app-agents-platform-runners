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
2. Register a tiny /status route (routes.py) that checks whether
   claude/codex/copilot/cursor-agent actually resolve on PATH and reports
   their version — a live signal that the code-agent-clis dependency
   (aw-app.json dependencies.apps, required) actually did its job. This
   app never installs those CLIs itself; depending on code-agent-clis is
   the reused path (Frederico decision 2026-08-01) instead of duplicating
   install logic in a second place (e.g. agents-platform_multitenant's own
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
from . import routes as routes_mod
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

# agents-platform_multitenant now runs as its own docker-compose stack
# (repos/agents-platform_multitenant/docker-compose.yml), attached to the
# `agentic-workspace_default` bridge network and publishing :10014 on the
# real host — decoupled from ./aw (2026-08-02). This app's MCP server runs
# inside aw-app-mcp-gateway, itself nested one level deeper (podman inside
# the aw-remote-host container), so `127.0.0.1` / `localhost` and even
# Docker DNS names on that bridge network (e.g. `agents-platform-multitenant`)
# don't resolve there — only the bridge's gateway IP is reachable from that
# deep. 172.18.0.1 is `agentic-workspace_default`'s gateway (== the real
# host's own address on that network) — verified reachable end-to-end from
# inside aw-app-mcp-gateway, 2026-08-02.
DEFAULT_AGENTS_PLATFORM_BASE = "http://172.18.0.1:10014"


def build_mcp_servers(config: dict) -> dict:
    """The ``mcpServers`` object this app's own root mcp.json should
    contain — one server, the ported agent_mcp.py, pointed at
    agents_platform_base. This exact file is what aw-mcp-gateway's
    app-scan reads directly (same contract aw-app-mcp-tools' mcp.json
    uses)."""
    config = config or {}
    base = config.get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE
    token = config.get("agents_platform_token") or ""
    return {
        "agents-platform-runners": {
            "enabled": True,
            "type": "stdio",
            "command": "python3",
            "args": ["-m", "agents_platform_runners_app.mcp_server"],
            # agents-platform_multitenant's require_identity() rejects every
            # request without an aw-backend identity JWT (401) — this is
            # that credential (mcp_server.py sends it as Authorization:
            # Bearer on every call). Mint one with aw-backend's
            # create_identity_jwt(); see this app's README for the command.
            "env": {"AGENTS_BASE": str(base), "AGENTS_PLATFORM_TOKEN": str(token)},
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

    async def activate(self, ctx) -> None:
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            json.load(f)  # validated at install time — just confirms the file is readable here

        config = getattr(ctx, "config", {}) or {}
        self._live_config.clear()
        self._live_config.update(config)
        mcp_doc = write_mcp_json(ctx.package_dir, self._live_config)

        ctx.routes.register(routes_mod.build_routes(self._live_config))

        self._register_skills_watchdog(ctx, self._live_config)

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
        base = config.get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE
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
            result = await asyncio.to_thread(client.sync_full)
            log.info("skills_sync reconcile: %s", result)

        ctx.watchdog.register("skills-sync-delta", _delta, DELTA_INTERVAL_S,
                              run_immediately=True)
        ctx.watchdog.register("skills-sync-reconcile", _reconcile, RECONCILE_INTERVAL_S,
                              run_immediately=False)
        log.info("skills_sync: watchdog registered (workspace=%s base=%s)", workspace, base)

    def register_contributed_agents(self, app_id: str, spec: dict) -> dict:
        """Seed one ``contributes.agents`` declaration into Agents Platform.

        This is the provider side of aw-workspace's agent-contribution
        protocol (its ``src/apps/agents.py``): any installed app declares
        the models, agent configs, groups and agents its features need, and
        the workspace hands the whole declaration here on activation — as
        one call, so this side owns the creation ORDER an Agent's slug
        references depend on.

        **Create-if-absent, matched by slug, never updated**, exactly like
        the tasks surface: an existing object is left as it is, prompt and
        model and all. See ``agent_provisioner.py`` for why a 409 counts as
        already-there rather than an error.

        Reads ``self._live_config``, not a snapshot, so a token pasted into
        the settings panel after this app came up is used by the next
        activation without a workspace restart.

        Called from aw-workspace's synchronous activation path, which
        already guards against exceptions; raising here is safe but
        pointless.
        """
        config = self._live_config or {}
        provisioner = agent_provisioner_mod.AgentProvisioner(
            base=config.get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE,
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
        effect."""
        config = getattr(ctx, "config", {}) or {}
        self._live_config.clear()
        self._live_config.update(config)
        mcp_doc = write_mcp_json(ctx.package_dir, self._live_config)
        log.info("aw-app-agents-platform-runners config saved: mcp.json servers=%s", list(mcp_doc["mcpServers"]))

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

    async def deactivate(self) -> None:
        log.info("aw-app-agents-platform-runners deactivated")
