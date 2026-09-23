"""Regression coverage for the Kanban 3e45bf3b-9510-816d-b0e3-d94995d2168c
fix: AP-MT runner ``/execute`` 401s recurring because a freshly-promoted
RedisLease("core") leader's ``identity-token-refresh`` watchdog used to be
``run_immediately=False`` — WatchdogSupervisor.resume() (aw-workspace core's
``src/apps/watchdog.py``) restarts a ``run_immediately=False`` task's
sleep-then-tick loop from scratch on every leadership handoff, so a leader
whose own cached ``agents_platform_token`` was already stale would not
re-check staleness for up to a further 6h (IDENTITY_TOKEN_INTERVAL_S) — long
enough for the pushed token to cross its 24h expiry and 401 every /execute
dispatch in that window. Flipping the registration to run_immediately=True
makes a freshly-promoted leader check ``needs_refresh()`` on its very first
tick instead — see WatchdogSupervisor._run()'s honouring of
``t.run_immediately`` on every resume(), not just initial registration.

Run: .venv/aw/bin/python -m pytest tests/test_identity_token_watchdog.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import plugin as plugin_mod  # noqa: E402
from agents_platform_runners_app.plugin import AgentsPlatformRunnersAppPlugin  # noqa: E402


class _StubWatchdog:
    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def register(self, name, fn, interval, run_immediately=False) -> None:
        self.registered.append((name, fn, interval, run_immediately))


class _StubCtx:
    def __init__(self, config: dict, *, has_watchdog_cap: bool = True) -> None:
        self.config = config
        self.watchdog = _StubWatchdog()
        self._has_watchdog_cap = has_watchdog_cap

    def has(self, capability: str) -> bool:
        if capability == "watchdog:tasks":
            return self._has_watchdog_cap
        return False


def test_watchdog_not_registered_without_capability():
    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({}, has_watchdog_cap=False)

    plugin._register_identity_token_watchdog(ctx, {})

    assert ctx.watchdog.registered == []


def test_watchdog_registers_with_run_immediately_true():
    """The actual fix: a stale token on a freshly-promoted leader must be
    caught on its first tick, not up to IDENTITY_TOKEN_INTERVAL_S (6h) later
    — this is what makes WatchdogSupervisor.resume() (core) tick this task
    immediately on every RedisLease("core") acquisition, not just once at
    process boot."""
    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})

    plugin._register_identity_token_watchdog(ctx, {})

    assert len(ctx.watchdog.registered) == 1
    name, _fn, interval, run_immediately = ctx.watchdog.registered[0]
    assert name == "identity-token-refresh"
    assert interval == plugin_mod.IDENTITY_TOKEN_INTERVAL_S
    assert run_immediately is True


def test_tick_refreshes_and_updates_live_config(monkeypatch):
    calls = []

    def fake_refresh(config):
        calls.append(dict(config))
        return "freshly-refreshed"

    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", fake_refresh)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "stale-tok-past-half-life"
    ctx = _StubCtx({})
    plugin._register_identity_token_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, run_immediately = ctx.watchdog.registered[0]

    assert run_immediately is True  # this tick fires on leader acquisition, not after a sleep
    asyncio.run(tick())

    assert plugin._live_config["agents_platform_token"] == "freshly-refreshed"
    assert len(calls) == 1
    assert calls[0]["agents_platform_token"] == "stale-tok-past-half-life"


def test_tick_is_a_noop_when_token_not_due(monkeypatch):
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "fresh-tok"
    ctx = _StubCtx({})
    plugin._register_identity_token_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())

    assert plugin._live_config["agents_platform_token"] == "fresh-tok"


def test_tick_survives_refresh_raising(monkeypatch):
    def _boom(config):
        raise RuntimeError("aw-backend unreachable")

    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_identity_token_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())  # must not raise
