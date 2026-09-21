"""Unit tests for kanban_sweep_default.py (Kanban "auto-criar sweep de Ready
cards + automatizar/verificar webhook do Notion") — the one-time flip that
turns kanban_sweep_enabled on for a fresh install. Same stub-the-transport
style as test_execute_secret.py. No real network, no workspace API.

Run: .venv/aw/bin/python -m pytest tests/test_kanban_sweep_default.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import kanban_sweep_default as ksd  # noqa: E402
from agents_platform_runners_app import plugin as plugin_mod  # noqa: E402
from agents_platform_runners_app.plugin import AgentsPlatformRunnersAppPlugin  # noqa: E402

ENV = {
    "AW_WORKSPACE_API_URL": "http://aw-workspace-api",
    "AW_WORKSPACE_API_KEY": "wsapikey",
}


@pytest.fixture(autouse=True)
def _workspace_env_stub(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: ENV.get(name, ""))


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _stub_client(monkeypatch, responder, recorder=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            if recorder is not None:
                recorder.append((url, kwargs))
            result = responder(url, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(ksd.httpx, "Client", _Client)


# --- ensure_default_applied(): the flip + persist round trip ----------------


def test_new_install_flips_the_default_and_persists(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"config": {}}), seen)

    result = ksd.ensure_default_applied({})

    assert result == {"kanban_sweep_enabled": True, "kanban_sweep_default_applied": True}
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url == "http://aw-workspace-api/api/apps/agents-platform-runners/config"
    assert kwargs["headers"] == {"X-Api-Key": "wsapikey"}
    assert kwargs["json"] == {"config": {"kanban_sweep_enabled": True,
                                         "kanban_sweep_default_applied": True}}


def test_already_applied_is_left_untouched_no_network(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)
    config = {"kanban_sweep_default_applied": True, "kanban_sweep_enabled": False}

    result = ksd.ensure_default_applied(config)

    assert result is None
    assert config == {"kanban_sweep_default_applied": True, "kanban_sweep_enabled": False}
    assert seen == []


def test_a_human_untick_after_the_flip_is_never_reasserted(monkeypatch):
    """The whole point of the guard field: once applied, kanban_sweep_enabled
    being false again (a deliberate untick) must never trigger a re-flip —
    that untick IS the user's entire rollback."""
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)
    config = {"kanban_sweep_default_applied": True, "kanban_sweep_enabled": False}

    for _ in range(3):
        assert ksd.ensure_default_applied(config) is None
    assert seen == []


def test_persist_failure_returns_none_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))
    config = {}

    result = ksd.ensure_default_applied(config)

    assert result is None
    assert config == {}


def test_persist_connection_error_returns_none_no_raise(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: httpx.ConnectError("connection refused"))

    result = ksd.ensure_default_applied({})

    assert result is None


def test_missing_workspace_env_returns_none_no_network(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: "")
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    result = ksd.ensure_default_applied({})

    assert result is None
    assert seen == []


# --- activate() integration: the flip lands before the watchdog registers --


class _StubRoutes:
    def register(self, app) -> None:
        pass


class _StubWatchdog:
    def register(self, name, fn, interval, run_immediately=False) -> None:
        pass


class _StubCtx:
    def __init__(self, config: dict) -> None:
        self.package_dir = str(ROOT)
        self.config = config
        self.routes = _StubRoutes()
        self.watchdog = _StubWatchdog()

    def has(self, capability: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def _silence_other_activate_side_effects(monkeypatch):
    monkeypatch.setattr(plugin_mod, "write_mcp_json", lambda package_dir, config: {"mcpServers": {}})
    monkeypatch.setattr(plugin_mod.execute_mod, "_reap_isolated_dirs_all", lambda: None)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "configure", lambda config: False)
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)
    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", lambda config: None)
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: {"registered": {}})


def test_new_workspace_activate_flips_default_before_watchdog_registration(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"config": {}}), seen)

    seen_config_at_registration = {}

    def spy_register_kanban_sweep(ctx, config):
        seen_config_at_registration.update(config)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = spy_register_kanban_sweep
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    plugin._register_runner_registration_watchdog = lambda ctx, config: None
    plugin._register_execute_secret_watchdog = lambda ctx, config: None
    ctx = _StubCtx({})  # brand new workspace: no config persisted yet

    asyncio.run(plugin.activate(ctx))

    assert len(seen) == 1  # persisted exactly once, before the watchdog saw the config
    assert seen_config_at_registration["kanban_sweep_enabled"] is True
    assert seen_config_at_registration["kanban_sweep_default_applied"] is True
    assert plugin._live_config["kanban_sweep_enabled"] is True


def test_activate_never_reflips_an_already_applied_install(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = lambda ctx, config: None
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    plugin._register_runner_registration_watchdog = lambda ctx, config: None
    plugin._register_execute_secret_watchdog = lambda ctx, config: None
    ctx = _StubCtx({"kanban_sweep_default_applied": True, "kanban_sweep_enabled": False})

    asyncio.run(plugin.activate(ctx))

    assert seen == []  # never called the persist endpoint
    assert plugin._live_config["kanban_sweep_enabled"] is False


def test_activate_completes_when_flip_fails(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = lambda ctx, config: None
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    plugin._register_runner_registration_watchdog = lambda ctx, config: None
    plugin._register_execute_secret_watchdog = lambda ctx, config: None
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise


def test_activate_completes_when_flip_raises(monkeypatch):
    def _boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.kanban_sweep_default_mod, "ensure_default_applied", _boom)

    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = lambda ctx, config: None
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    plugin._register_runner_registration_watchdog = lambda ctx, config: None
    plugin._register_execute_secret_watchdog = lambda ctx, config: None
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise
