"""Unit tests for execute_secret.py (Kanban "execute_secret nunca é
auto-gerado — runner falha com 500 numa workspace nova") — the
generate+persist logic that seeds ``execute_secret`` on a brand new
workspace. Same stub-the-transport style as test_identity_token.py. No real
network, no workspace API.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute_secret as es  # noqa: E402
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
    """Replace httpx.Client inside execute_secret with one whose .post(url,
    **kwargs) is answered by responder(url, kwargs) — a FakeResponse, or an
    exception instance to raise."""
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

    monkeypatch.setattr(es.httpx, "Client", _Client)


# --- ensure_configured(): the generate + persist round trip -----------------


def test_new_workspace_generates_and_persists_a_secret(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"config": {}}), seen)

    result = es.ensure_configured({})

    assert result is not None
    assert len(result) >= 32  # secrets.token_urlsafe(32) is well over this
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url == "http://aw-workspace-api/api/apps/agents-platform-runners/config"
    assert kwargs["headers"] == {"X-Api-Key": "wsapikey"}
    assert kwargs["json"] == {"config": {"execute_secret": result}}


def test_already_configured_is_left_untouched_no_network(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)
    config = {"execute_secret": "already-here"}

    result = es.ensure_configured(config)

    assert result is None
    assert config == {"execute_secret": "already-here"}
    assert seen == []


def test_generated_secrets_are_not_predictable(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}))

    first = es.ensure_configured({})
    second = es.ensure_configured({})

    assert first != second


def test_persist_failure_returns_none_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))
    config = {}

    result = es.ensure_configured(config)

    assert result is None
    assert config == {}


def test_persist_connection_error_returns_none_no_raise(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: httpx.ConnectError("connection refused"))

    result = es.ensure_configured({})

    assert result is None


def test_missing_workspace_env_returns_none_no_network(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: "")
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    result = es.ensure_configured({})

    assert result is None
    assert seen == []


# --- activate() integration: new workspace -> generated -> registered ------


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


def _plugin_with_stubbed_watchdogs() -> AgentsPlatformRunnersAppPlugin:
    plugin = AgentsPlatformRunnersAppPlugin()
    plugin._register_skills_watchdog = lambda ctx, config: None
    plugin._register_kanban_sweep_watchdog = lambda ctx, config: None
    plugin._register_identity_token_watchdog = lambda ctx, config: None
    plugin._register_runner_registration_watchdog = lambda ctx, config: None
    return plugin


@pytest.fixture(autouse=True)
def _silence_other_activate_side_effects(monkeypatch):
    """Everything in activate() that isn't this feature: stubbed out so the
    test exercises only the "config has no execute_secret" -> generate ->
    register ordering, not disk/network paths already covered elsewhere
    (mcp.json writes, warm-pool bookkeeping, isolated-dir reaping)."""
    monkeypatch.setattr(plugin_mod, "write_mcp_json", lambda package_dir, config: {"mcpServers": {}})
    monkeypatch.setattr(plugin_mod.execute_mod, "_reap_isolated_dirs_all", lambda: None)
    monkeypatch.setattr(plugin_mod.warm_pool_mod, "configure", lambda config: False)
    monkeypatch.setattr(plugin_mod.identity_token_mod, "refresh", lambda config: None)


def test_new_workspace_activate_generates_secret_before_first_registration(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {"config": {}}), seen)

    register_calls = []

    def fake_register(config):
        register_calls.append(dict(config))
        return {"registered": {"ok": True}}

    monkeypatch.setattr(plugin_mod.runner_registration_mod, "register_with_platform", fake_register)

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})  # brand new workspace: no execute_secret configured

    asyncio.run(plugin.activate(ctx))

    assert len(register_calls) == 1
    sent_secret = register_calls[0].get("execute_secret")
    assert sent_secret  # non-empty — the exact 500 this fixes was `execute_secret: None`
    assert plugin._live_config["execute_secret"] == sent_secret


def test_activate_never_overwrites_an_already_configured_secret(monkeypatch):
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    register_calls = []
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: register_calls.append(dict(config)) or {"registered": {}})

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({"execute_secret": "human-typed-value"})

    asyncio.run(plugin.activate(ctx))

    assert seen == []  # never called the persist endpoint
    assert register_calls[0]["execute_secret"] == "human-typed-value"


def test_activate_completes_when_generation_fails(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: {"registered": {}})

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise


def test_activate_completes_when_generation_raises(monkeypatch):
    def _boom(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod.execute_secret_mod, "ensure_configured", _boom)
    monkeypatch.setattr(
        plugin_mod.runner_registration_mod, "register_with_platform",
        lambda config: {"registered": {}})

    plugin = _plugin_with_stubbed_watchdogs()
    ctx = _StubCtx({})

    asyncio.run(plugin.activate(ctx))  # must not raise
