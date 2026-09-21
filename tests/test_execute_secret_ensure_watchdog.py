"""Coverage for the execute-secret-ensure-configured watchdog — the fix for
a gap the 2026-09-18 aw-claude 401 incident's own fixes did not close:
execute_secret.ensure_configured() was (and, outside this watchdog, still
is) only ever called once, from activate(). On a brand new workspace where
AW_WORKSPACE_API_KEY/AW_WORKSPACE_API_URL are not yet readable at that exact
moment (a boot-ordering race this app does not control), ensure_configured()
gives up silently and nothing ever asks again until the app's next full
restart/upgrade — which, for a long-lived workspace, could be arbitrarily
far away. This watchdog keeps retrying on a short cadence instead.

Run: .venv/aw/bin/python -m pytest tests/test_execute_secret_ensure_watchdog.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

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

    plugin._register_execute_secret_watchdog(ctx, {})

    assert ctx.watchdog.registered == []


def test_watchdog_registered_with_its_own_short_cadence():
    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})

    plugin._register_execute_secret_watchdog(ctx, {})

    assert len(ctx.watchdog.registered) == 1
    name, _fn, interval, run_immediately = ctx.watchdog.registered[0]
    assert name == "execute-secret-ensure-configured"
    assert interval == plugin_mod.EXECUTE_SECRET_ENSURE_INTERVAL_S
    assert run_immediately is False


def test_tick_generates_secret_and_reasserts_registration_immediately(monkeypatch):
    register_calls = []
    monkeypatch.setattr(
        plugin_mod.execute_secret_mod, "ensure_configured",
        lambda config: "freshly-generated-secret")
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (register_calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_execute_secret_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())

    assert plugin._live_config["execute_secret"] == "freshly-generated-secret"
    # Reasserted THIS tick, not left for the separate runner-registration
    # watchdog's own next (up to 120s away) cycle.
    assert len(register_calls) == 1
    assert register_calls[0]["execute_secret"] == "freshly-generated-secret"


def test_tick_is_a_noop_when_ensure_configured_returns_none(monkeypatch):
    """Already configured, or still can't reach AW_WORKSPACE_API_KEY/_URL —
    either way, ensure_configured() returning None means nothing changed, so
    there is nothing new to reassert. Confirms this watchdog does not spam a
    registration call on every tick once it has nothing left to do."""
    register_calls = []
    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", lambda config: None)
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (register_calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_execute_secret_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())

    assert "execute_secret" not in plugin._live_config
    assert register_calls == []


def test_tick_survives_ensure_configured_raising(monkeypatch):
    def _boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_execute_secret_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())  # must not raise


def test_tick_survives_reassert_after_ensure_raising(monkeypatch):
    """ensure_configured() succeeds, but the immediate follow-up reassert
    blows up — the generated secret must still be kept (it IS persisted on
    the workspace side already), and the failure must not propagate; the
    separate runner-registration-reassert watchdog still covers this."""
    monkeypatch.setattr(
        plugin_mod.execute_secret_mod, "ensure_configured", lambda config: "new-secret")

    def _boom(config):
        raise RuntimeError("agents-platform-multitenant unreachable")

    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_execute_secret_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())  # must not raise

    assert plugin._live_config["execute_secret"] == "new-secret"


def test_activate_registers_the_ensure_configured_watchdog(monkeypatch):
    """activate() wires this watchdog up alongside the other three — a
    regression here would silently drop the whole retry mechanism."""
    monkeypatch.setattr(plugin_mod, "write_mcp_json", lambda package_dir, config: {"mcpServers": {}})
    monkeypatch.setattr(plugin_mod.execute_mod, "_reap_isolated_dirs_all", lambda: None)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "configure", lambda config: False)
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: {"registered": {}})
    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", lambda config: None)
    monkeypatch.setattr(plugin_mod.kanban_sweep_default_mod, "ensure_default_applied",
                        lambda config: None)

    class _StubRoutes:
        def register(self, app) -> None:
            pass

    class _ActivateCtx(_StubCtx):
        def __init__(self, config: dict) -> None:
            super().__init__(config)
            self.package_dir = str(ROOT)
            self.routes = _StubRoutes()

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _ActivateCtx({})

    asyncio.run(plugin.activate(ctx))

    names = [name for name, *_ in ctx.watchdog.registered]
    assert "execute-secret-ensure-configured" in names
