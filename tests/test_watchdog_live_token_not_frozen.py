"""Regression coverage for the Kanban 3e45bf3b-9510-816d-b0e3-d94995d2168c
fix: ``_register_skills_watchdog``'s ``_delta``/``_reconcile`` closures (and
``_register_kanban_sweep_watchdog``'s ``_sweep`` closure) used to bake
``agents_platform_token`` into a plain local variable / SkillsSyncClient
attribute at ``activate()`` time and never look at it again — unlike every
other credential watchdog in plugin.py (``_refresh``/``_reassert``/
``_ensure``), which all read ``self._live_config[...]`` live on each tick.
Caught live via ``GET /api/apps/-/watchdog``: one worker showed
skills-sync-delta/reconcile at 23/22 consecutive 401s against
``POST /api/runners/skills/sync`` while runner-registration-reassert (a
live-read watchdog) was ``ok:true`` on the same snapshot — once ~24h passed
since that worker's own activate(), its frozen token stayed expired for the
rest of that worker's life, with no fresh app config, token rotation, or
even a full re-registration able to fix it short of a process restart.

Run: .venv/aw/bin/python -m pytest tests/test_watchdog_live_token_not_frozen.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from agents_platform_runners_app import plugin as plugin_mod  # noqa: E402
from agents_platform_runners_app.plugin import AgentsPlatformRunnersAppPlugin  # noqa: E402


class _StubWatchdog:
    def __init__(self) -> None:
        self.registered: dict[str, tuple] = {}

    def register(self, name, fn, interval, run_immediately=False) -> None:
        self.registered[name] = (fn, interval, run_immediately)


class _StubCtx:
    def __init__(self, config: dict, *, has_watchdog_cap: bool = True) -> None:
        self.config = config
        self.watchdog = _StubWatchdog()
        self._has_watchdog_cap = has_watchdog_cap

    def has(self, capability: str) -> bool:
        if capability == "watchdog:tasks":
            return self._has_watchdog_cap
        return False


# --- skills-sync: SkillsSyncClient must reflect a config-updated token -----


def test_skills_delta_tick_uses_the_live_config_token_not_the_frozen_one(monkeypatch):
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "activation-time-token"
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_skills_watchdog(ctx, plugin._live_config)
    delta_fn, _interval, _run_immediately = ctx.watchdog.registered["skills-sync-delta"]

    # No app config save, no fresh watchdog registration — just the config
    # dict the token-refresh watchdog mutates in place on a later tick.
    plugin._live_config["agents_platform_token"] = "rotated-by-refresh-watchdog"

    seen = {}
    monkeypatch.setattr(plugin_mod.skills_sync_mod.SkillsSyncClient, "sync_incremental",
                        lambda self: seen.setdefault("token", self.token) or {"mode": "delta"})

    asyncio.run(delta_fn())

    assert seen["token"] == "rotated-by-refresh-watchdog"


def test_skills_reconcile_tick_uses_the_live_config_token(monkeypatch):
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)
    monkeypatch.setattr(plugin_mod.notion_token_sync_mod, "reconcile_once",
                        lambda config: {"reconciled": True})

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "activation-time-token"
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_skills_watchdog(ctx, plugin._live_config)
    reconcile_fn, _interval, _run_immediately = ctx.watchdog.registered["skills-sync-reconcile"]

    plugin._live_config["agents_platform_token"] = "rotated-by-refresh-watchdog"

    seen = {}
    monkeypatch.setattr(plugin_mod.skills_sync_mod.SkillsSyncClient, "sync_full",
                        lambda self: seen.setdefault("token", self.token) or {"mode": "full"})

    asyncio.run(reconcile_fn())

    assert seen["token"] == "rotated-by-refresh-watchdog"


def test_skills_tick_keeps_last_known_token_if_config_value_goes_missing(monkeypatch):
    """A momentarily-empty config value (mid on_config_reloaded mutation) must
    not blank out a client that was working — fall back to whatever the
    client already had rather than sending an empty Authorization header."""
    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "activation-time-token"
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_skills_watchdog(ctx, plugin._live_config)
    delta_fn, _interval, _run_immediately = ctx.watchdog.registered["skills-sync-delta"]

    plugin._live_config.pop("agents_platform_token")

    seen = {}
    monkeypatch.setattr(plugin_mod.skills_sync_mod.SkillsSyncClient, "sync_incremental",
                        lambda self: seen.setdefault("token", self.token) or {"mode": "delta"})

    asyncio.run(delta_fn())

    assert seen["token"] == "activation-time-token"


# --- kanban sweep: same frozen-token pattern in a second watchdog ----------


class _FakeAsyncClient:
    captured_headers: list[dict] = []

    def __init__(self, *, timeout, headers):
        _FakeAsyncClient.captured_headers.append(dict(headers))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_kanban_sweep_tick_uses_the_live_config_token(monkeypatch):
    _FakeAsyncClient.captured_headers = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    async def _fake_sweep_ready(board, platform):
        return {"considered": 0}

    monkeypatch.setattr(plugin_mod.kanban_dispatch_mod, "sweep_ready", _fake_sweep_ready)
    monkeypatch.setattr(plugin_mod.kanban_dispatch_mod, "PlatformClient",
                        lambda client, base: object())

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "activation-time-token"
    plugin._live_config["kanban_sweep_enabled"] = True
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_kanban_sweep_watchdog(ctx, plugin._live_config)
    sweep_fn, _interval, _run_immediately = ctx.watchdog.registered["kanban-ready-sweep"]

    plugin._live_config["agents_platform_token"] = "rotated-by-refresh-watchdog"

    asyncio.run(sweep_fn())

    assert _FakeAsyncClient.captured_headers == [
        {"Authorization": "Bearer rotated-by-refresh-watchdog"}
    ]
