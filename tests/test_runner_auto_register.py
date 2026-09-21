"""Coverage for Kanban feature:ap-runners-auto-register-on-activation — the
POST /register logic (routes.py, extracted to runner_registration.py) is now
also called automatically from activate() right after the identity token is
confirmed fresh, and reasserted on a periodic watchdog. Frederico's original
ask (Telegram, PT-BR): "ele pode automaticamente registrar os runners na
instalação tb, dai ele já sobe os runners da workspace".

Run: .venv/aw/bin/python -m pytest tests/test_runner_auto_register.py
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


class _StubRoutes:
    def register(self, app) -> None:
        pass


class _StubWatchdog:
    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def register(self, name, fn, interval, run_immediately=False) -> None:
        self.registered.append((name, fn, interval, run_immediately))


class _StubCtx:
    def __init__(self, config: dict, *, has_watchdog_cap: bool = True) -> None:
        self.package_dir = str(ROOT)
        self.config = config
        self.routes = _StubRoutes()
        self.watchdog = _StubWatchdog()
        self._has_watchdog_cap = has_watchdog_cap

    def has(self, capability: str) -> bool:
        if capability == "watchdog:tasks":
            return self._has_watchdog_cap
        return False


@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch):
    """Everything in activate() that isn't this feature: stubbed out so the
    test exercises only the token-refresh -> register ordering and its
    non-fatal-failure behaviour, not disk/network paths already covered
    elsewhere (mcp.json writes, warm-pool bookkeeping, isolated-dir reaping)."""
    monkeypatch.setattr(plugin_mod, "write_mcp_json", lambda package_dir, config: {"mcpServers": {}})
    monkeypatch.setattr(plugin_mod.execute_mod, "_reap_isolated_dirs_all", lambda: None)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "configure", lambda config: False)
    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", lambda config: None)
    monkeypatch.setattr(plugin_mod.kanban_sweep_default_mod, "ensure_default_applied",
                        lambda config: None)


def _plugin_with_stubbed_watchdogs() -> AgentsPlatformRunnersAppPlugin:
    """A plugin whose OTHER three watchdog registrations are no-ops, so a
    test only has to reason about the runner-registration one."""
    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = lambda ctx, config: None
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    return plugin


# --- activate() calls register_with_platform after the token is fresh ------


def test_activate_registers_runners_after_token_refresh(monkeypatch):
    calls = []

    def fake_refresh(config):
        config["agents_platform_token"] = "freshly-minted"
        return "freshly-minted"

    def fake_register(config):
        calls.append(dict(config))
        return {"registered": {"ok": True}}

    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", fake_refresh)
    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", fake_register)

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})
    asyncio.run(plugin.activate(ctx))

    assert len(calls) == 1
    # The token minted by refresh() must already be in the config register
    # was called with — proves the ordering, not just that both ran.
    assert calls[0]["agents_platform_token"] == "freshly-minted"


def test_activate_completes_when_registration_fails(monkeypatch):
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: {"error": "agents-platform-multitenant unreachable"})

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise

    assert ("runner-registration-reassert",) == tuple(
        name for name, *_ in ctx.watchdog.registered if name == "runner-registration-reassert")


def test_activate_completes_when_registration_raises(monkeypatch):
    """Non-fatal means non-fatal even on an exception, not just an error dict
    — mirrors the identity_token refresh's own try/except in activate()."""
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)

    def _boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", _boom)

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise


# --- the periodic reassert watchdog -----------------------------------------


def test_watchdog_not_registered_without_capability():
    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({}, has_watchdog_cap=False)

    plugin._register_runner_registration_watchdog(ctx, {})

    assert ctx.watchdog.registered == []


def test_watchdog_registered_with_its_own_short_cadence():
    """Deliberately NOT the identity-token cadence (6h) — see
    RUNNER_REGISTRATION_REASSERT_INTERVAL_S's docstring for the 2026-09-18
    incident this separation closes: a wrong execute_secret/caller_token
    401s every single dispatch immediately, so this backstop needs to be
    short (minutes), unlike a 24h JWT with plenty of runway."""
    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})

    plugin._register_runner_registration_watchdog(ctx, {})

    assert len(ctx.watchdog.registered) == 1
    name, _fn, interval, run_immediately = ctx.watchdog.registered[0]
    assert name == "runner-registration-reassert"
    assert interval == plugin_mod.RUNNER_REGISTRATION_REASSERT_INTERVAL_S
    assert interval < plugin_mod.IDENTITY_TOKEN_INTERVAL_S
    assert run_immediately is False


def test_watchdog_tick_reasserts_registration(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (calls.append(config) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "tok"
    ctx = _StubCtx({})

    plugin._register_runner_registration_watchdog(ctx, plugin._live_config)
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())
    asyncio.run(tick())

    assert len(calls) == 2  # reasserted on every tick, not just once


def test_watchdog_tick_survives_registration_raising(monkeypatch):
    def _boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})
    plugin._register_runner_registration_watchdog(ctx, {})
    _name, tick, _interval, _run_immediately = ctx.watchdog.registered[0]

    asyncio.run(tick())  # must not raise


def test_skills_watchdog_reads_workspace_from_the_env_file_not_just_process_env(
        monkeypatch, tmp_path):
    """runner-dynamic-workspace-slug (Perna C): a raw ``os.environ.get`` here
    tags this workspace's skills-index sync as "aw" whenever ``AW_WORKSPACE``
    lives only in ``.aw-workspace/.env`` — the skills registry keys on
    (workspace, cli), so that silently misfiles this workspace's own skill
    index under another workspace's row."""
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("AW_WORKSPACE=crispal\n")

    seen = {}
    monkeypatch.setattr(plugin_mod.skills_sync_mod, "SkillsSyncClient",
                        lambda base, token, workspace: seen.setdefault("workspace", workspace))

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({"agents_platform_token": "tok"})
    plugin._register_skills_watchdog(ctx, {"agents_platform_token": "tok"})

    assert seen["workspace"] == "crispal"
