"""Regression coverage for the Kanban 3e45bf3b-9510-816d-b0e3-d94995d2168c
fix: the skills-index ``_delta``/``_reconcile`` closures (and
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

The skills-index half of that fix is gone with the index itself (aw-skills
fase 5, DESIGN §17.6 step 1). What replaced its registration — the
Notion-token reconcile, which was riding its tick — is covered below in the
same terms: it must hand ``_live_config`` itself to ``reconcile_once``, not a
snapshot taken at activate() time. The kanban-sweep half is untouched.

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


# --- notion-token reconcile: must see a config-updated token ---------------


def test_notion_reconcile_tick_gets_the_live_config_dict_not_a_snapshot(monkeypatch):
    """The reconcile closure must pass ``self._live_config`` itself, so a token
    the refresh watchdog rotated in place on a later tick is the one
    ``reconcile_once`` sees — the same live-read rule the skills-sync closures
    this registration replaced had to be fixed to follow."""
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "activation-time-token"
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_notion_token_watchdog(ctx, plugin._live_config)
    reconcile_fn, interval, run_immediately = ctx.watchdog.registered["notion-token-reconcile"]

    # Same cadence the skills-sync reconcile it inherited from used, and still
    # not run_immediately — this is a reconcile, not a boot-time sync.
    assert interval == plugin_mod.NOTION_TOKEN_RECONCILE_INTERVAL_S
    assert run_immediately is False

    # No app config save, no fresh watchdog registration — just the config
    # dict the token-refresh watchdog mutates in place on a later tick.
    plugin._live_config["agents_platform_token"] = "rotated-by-refresh-watchdog"

    seen = {}

    def _record(config):
        seen["token"] = config.get("agents_platform_token")
        return {"reconciled": True}

    monkeypatch.setattr(plugin_mod.notion_token_sync_mod, "reconcile_once", _record)

    asyncio.run(reconcile_fn())

    assert seen["token"] == "rotated-by-refresh-watchdog"


def test_notion_reconcile_watchdog_is_registered_without_the_dead_skills_tasks(monkeypatch):
    """aw-skills fase 5 (DESIGN §17.6 step 1): the two skills-index tasks are
    gone and must not come back — a workspace still POSTing a slug list to an
    AP-MT that answers 410 is the silent-degradation fuel the ordered
    decommission exists to avoid."""
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "tok"
    ctx = _StubCtx(dict(plugin._live_config))
    plugin._register_notion_token_watchdog(ctx, plugin._live_config)

    assert "notion-token-reconcile" in ctx.watchdog.registered
    assert "skills-sync-delta" not in ctx.watchdog.registered
    assert "skills-sync-reconcile" not in ctx.watchdog.registered
    assert not hasattr(plugin, "_register_skills_watchdog")
    assert not hasattr(plugin_mod, "skills_sync_mod")


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
