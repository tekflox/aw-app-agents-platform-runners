"""Regression coverage for the 2026-09-18 aw-claude 401 incident's actual
fix: on_config_saved now reasserts registration with agents-platform-
multitenant IMMEDIATELY whenever execute_secret or agents_platform_token
actually changes on a save, instead of waiting for the periodic reassert
watchdog (which inherited a 6h cadence from identity_token at the time of
the incident — see plugin.py's RUNNER_REGISTRATION_REASSERT_INTERVAL_S).

A config save is exactly the moment execute_secret.ensure_configured() (or a
human) changes one of these two values, so this is the fix at its source;
the watchdog (test_runner_auto_register.py) is the backstop for whatever
this misses (e.g. a value that changed without going through a save).

Run: .venv/aw/bin/python -m pytest tests/test_runner_registration_reassert_on_save.py
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


class _StubCtx:
    def __init__(self, config: dict) -> None:
        self.package_dir = str(ROOT)
        self.config = config


@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch):
    """Everything on_config_saved does that isn't this feature: stubbed out
    so a test only exercises the reassert-on-change logic, not disk/network
    paths already covered elsewhere."""
    monkeypatch.setattr(plugin_mod, "write_mcp_json", lambda package_dir, config: {"mcpServers": {}})
    monkeypatch.setattr(plugin_mod.platform_settings_mod, "push_settings", lambda **kwargs: None)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "configure", lambda config: False)


def _run_and_drain(coro) -> None:
    """Run ``coro`` to completion, then also drain any task it scheduled via
    asyncio.create_task (the background reassert) before the loop closes —
    plain asyncio.run() would otherwise tear the loop down with that task
    still pending, so it never actually executes."""
    async def _wrapper():
        await coro
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_wrapper())


def test_reassert_triggered_when_execute_secret_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["execute_secret"] = "old-secret"
    ctx = _StubCtx({"execute_secret": "new-secret"})

    _run_and_drain(plugin.on_config_saved(ctx))

    assert len(calls) == 1
    assert calls[0]["execute_secret"] == "new-secret"


def test_reassert_triggered_when_agents_platform_token_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["agents_platform_token"] = "old-tok"
    ctx = _StubCtx({"agents_platform_token": "new-tok"})

    _run_and_drain(plugin.on_config_saved(ctx))

    assert len(calls) == 1


def test_reassert_not_triggered_when_neither_credential_changed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["execute_secret"] = "same"
    plugin._live_config["agents_platform_token"] = "same-tok"
    plugin._live_config["agents_platform_base"] = "http://old.example"
    # A save that only touches an unrelated field (agents_platform_base)
    # must not trigger a reassert — the whole point is scoping this to the
    # two dispatch credentials, not every config save.
    ctx = _StubCtx({
        "execute_secret": "same", "agents_platform_token": "same-tok",
        "agents_platform_base": "http://new.example",
    })

    _run_and_drain(plugin.on_config_saved(ctx))

    assert calls == []


def test_reassert_not_triggered_on_first_save_with_nothing_configured(monkeypatch):
    """Both start absent (fresh workspace, before ensure_configured ever
    ran) and stay absent — no change, no reassert. Distinct from the
    execute_secret.ensure_configured()+activate() path, which is a separate,
    already-covered call site (test_runner_auto_register.py)."""
    calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: (calls.append(dict(config)) or {"registered": {}}))

    plugin = AgentsPlatformRunnersAppPlugin()
    ctx = _StubCtx({})

    _run_and_drain(plugin.on_config_saved(ctx))

    assert calls == []


def test_reassert_on_save_failure_is_non_fatal(monkeypatch):
    def _boom(config):
        raise RuntimeError("agents-platform-multitenant unreachable")

    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._live_config["execute_secret"] = "old-secret"
    ctx = _StubCtx({"execute_secret": "new-secret"})

    _run_and_drain(plugin.on_config_saved(ctx))  # must not raise
