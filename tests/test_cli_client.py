"""``agents_platform_runners_app.cli.client`` — config resolution, base-URL
resolution (must go through ``platform_base.resolve()``, never a raw config
read — Risk #1 in the Architect's design), and error mapping.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agents_platform_runners_app.cli import client as client_mod
from agents_platform_runners_app.cli.client import NotConfigured, PlatformClient, PlatformError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AW_AGENTS_PLATFORM_BASE", "AW_AGENTS_PLATFORM_TOKEN", "AW_BACKEND_URL",
                "AW_WORKSPACE_HOME", "AW_WORKSPACE_CONTAINER_DIR"):
        monkeypatch.delenv(var, raising=False)
    # Never let a test accidentally read this host's real 0600 config file —
    # point the default lookup at a directory that doesn't exist unless a
    # test explicitly overrides AW_WORKSPACE_HOME itself.
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/nonexistent-for-tests")


def test_base_url_resolution_ignores_the_legacy_literal(monkeypatch, tmp_path):
    """The exact regression the design doc calls out: a naive
    config["agents_platform_base"] read would return the legacy bridge
    address that is persisted on real hosts. PlatformClient must go through
    platform_base.resolve(), which treats it as unset."""
    home = tmp_path / "home"
    (home / "app-config").mkdir(parents=True)
    config = {
        "agents_platform_base": "http://172.18.0.1:10014",
        "agents_platform_token": "tok123",
    }
    (home / "app-config" / "agents-platform-runners.json").write_text(json.dumps(config))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(home))
    monkeypatch.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")

    c = PlatformClient()
    assert c.base == "https://agents-platform.aw.tekflox.com"
    assert c.token == "tok123"


def test_missing_config_file_raises_not_configured_on_request(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "nonexistent"))
    c = PlatformClient()
    with pytest.raises(NotConfigured):
        c.get("/api/agents")


def test_env_token_override_wins(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "app-config").mkdir(parents=True)
    (home / "app-config" / "agents-platform-runners.json").write_text(
        json.dumps({"agents_platform_token": "from-config"}))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(home))
    monkeypatch.setenv("AW_AGENTS_PLATFORM_TOKEN", "from-env")
    c = PlatformClient()
    assert c.token == "from-env"


def test_non_2xx_raises_platform_error_with_detail(monkeypatch):
    class _Resp:
        status_code = 409

        def json(self):
            return {"detail": "bot 'aw-17' already exists"}

        text = '{"detail": "bot \'aw-17\' already exists"}'
        content = b"x"

    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp())
    c = PlatformClient(base="https://example.test", token="tok")
    with pytest.raises(PlatformError) as exc_info:
        c.post("/api/telegram/bots", json_body={})
    assert exc_info.value.status == 409
    assert "already exists" in exc_info.value.body


def test_2xx_with_no_body_returns_none(monkeypatch):
    class _Resp:
        status_code = 204
        content = b""

        def json(self):
            raise ValueError("no body")

    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp())
    c = PlatformClient(base="https://example.test", token="tok")
    assert c.delete("/api/telegram/bots/aw-17") is None


def test_2xx_returns_parsed_json(monkeypatch):
    class _Resp:
        status_code = 200
        content = b'{"ok": true}'

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp())
    c = PlatformClient(base="https://example.test", token="tok")
    assert c.get("/api/agents") == {"ok": True}


def test_httpx_error_is_wrapped_as_platform_error(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "request", _raise)
    c = PlatformClient(base="https://example.test", token="tok")
    with pytest.raises(PlatformError):
        c.get("/api/agents")
