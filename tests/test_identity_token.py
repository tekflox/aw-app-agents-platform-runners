"""Unit tests for identity_token.py (Kanban
feature:ap-runners-auto-mint-identity-token) — the mint+persist half of the
auto-refreshed agents_platform_token. Same stub-the-transport style as
test_notion_token_sync.py. No real network, no aw-backend, no workspace API.
"""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from agents_platform_runners_app import identity_token as idt
from agents_platform_runners_app import plugin as plugin_mod

ENV = {
    "AW_BACKEND_URL": "http://aw-backend",
    "AW_WORKSPACE": "aw",
    "AW_WORKSPACE_HOST_TOKEN": "awlk_host_tok",
    "AW_WORKSPACE_API_URL": "http://aw-workspace-api",
    "AW_WORKSPACE_API_KEY": "wsapikey",
}


@pytest.fixture(autouse=True)
def _workspace_env_stub(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: ENV.get(name, ""))


def _token(iat: float, exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": iat, "exp": exp}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


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
    """Replace httpx.Client inside identity_token with one whose .post(url,
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

    monkeypatch.setattr(idt.httpx, "Client", _Client)


# --- needs_refresh: the decode + half-life policy, no network --------------


def test_no_token_configured_needs_refresh():
    assert idt.needs_refresh(None) is True
    assert idt.needs_refresh("") is True


def test_token_past_half_life_needs_refresh():
    now = 1_000_000.0
    token = _token(iat=now - 20 * 86400, exp=now + 10 * 86400)  # 20/30 days elapsed
    assert idt.needs_refresh(token, now=now) is True


def test_token_at_90pct_life_remaining_does_not_need_refresh():
    now = 1_000_000.0
    token = _token(iat=now - 3 * 86400, exp=now + 27 * 86400)  # 3/30 days elapsed = 10%
    assert idt.needs_refresh(token, now=now) is False


def test_garbage_non_jwt_token_needs_refresh_no_exception():
    assert idt.needs_refresh("not-a-jwt") is True
    assert idt.needs_refresh("a.b") is True
    assert idt.needs_refresh("a.b.c") is True
    assert idt.needs_refresh("...") is True


# --- refresh(): the mint + persist round trip --------------------------------


def test_refresh_attempts_mint_when_no_token_configured(monkeypatch):
    seen = []

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": _token(0, 9_999_999_999)})
        return FakeResponse(200, {})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is not None
    assert any(u.endswith("/api/workspaces/aw/identity-token") for u, _ in seen)


def test_refresh_not_attempted_with_plenty_of_life_left(monkeypatch):
    now = time.time()
    token = _token(iat=now - 3600, exp=now + 30 * 86400)  # far under half-life
    seen = []
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(200, {}), seen)

    result = idt.refresh({"agents_platform_token": token})

    assert result is None
    assert seen == []


def test_refresh_returns_none_on_backend_500_no_raise_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: FakeResponse(500, None, "boom"))
    config = {}

    result = idt.refresh(config)

    assert result is None
    assert config == {}


def test_refresh_returns_none_on_connection_error_no_raise_config_untouched(monkeypatch):
    _stub_client(monkeypatch, lambda url, kwargs: httpx.ConnectError("connection refused"))
    config = {"agents_platform_token": "garbage"}

    result = idt.refresh(config)

    assert result is None
    assert config == {"agents_platform_token": "garbage"}


def test_refresh_persists_via_own_config_endpoint_with_api_key_header(monkeypatch):
    seen = []

    def responder(url, kwargs):
        if url.endswith("/identity-token"):
            return FakeResponse(200, {"token": _token(0, 9_999_999_999)})
        return FakeResponse(200, {"config": {}})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is not None
    persist_calls = [(u, k) for u, k in seen if u.endswith("/config")]
    assert len(persist_calls) == 1
    url, kwargs = persist_calls[0]
    assert url == "http://aw-workspace-api/api/apps/agents-platform-runners/config"
    assert kwargs["headers"] == {"X-Api-Key": "wsapikey"}
    assert kwargs["json"] == {"config": {"agents_platform_token": result}}


def test_refresh_missing_env_vars_returns_none_no_raise(monkeypatch):
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: "")

    result = idt.refresh({})

    assert result is None


def test_refresh_returns_none_when_persist_target_env_missing(monkeypatch):
    partial_env = {**ENV, "AW_WORKSPACE_API_URL": "", "AW_WORKSPACE_API_KEY": ""}
    monkeypatch.setattr(plugin_mod, "_workspace_env", lambda name: partial_env.get(name, ""))

    seen = []

    def responder(url, kwargs):
        return FakeResponse(200, {"token": _token(0, 9_999_999_999)})

    _stub_client(monkeypatch, responder, seen)
    result = idt.refresh({})

    assert result is None
    # the mint was attempted, but nothing was ever posted to a /config URL
    assert not any(u.endswith("/config") for u, _ in seen)
