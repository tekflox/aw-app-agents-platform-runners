"""Unit tests for notion_token_sync.py and the /notion-token routes (Kanban
architecture:notion-token-per-tenant-ap-mt-step1) — the relay half: this app
owns the way to agents-platform-multitenant, aw-app-notion owns the token.

Same stub-the-transport style as test_observability_push.py. No real network,
no AP-MT instance, no aw-app-notion.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agents_platform_runners_app import notion_token_sync as nts
from agents_platform_runners_app import routes as routes_mod

CONFIG = {"agents_platform_token": "tok", "agents_platform_base": "http://ap-mt:10014"}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _stub_client(monkeypatch, response, recorder=None):
    """Replace httpx.Client inside notion_token_sync with one that records the
    request and answers `response` (or raises it, when it's an exception)."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, **kwargs):
            if recorder is not None:
                recorder.append((method, url, kwargs))
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(nts.httpx, "Client", _Client)


# --- the AP-MT leg ----------------------------------------------------------


def test_push_sends_the_workspace_and_bearer_token(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "ws-a")
    seen: list = []
    _stub_client(monkeypatch, FakeResponse(200, {"configured": True}), seen)

    assert nts.push(CONFIG, "ntn_abc") == {"configured": True}
    method, url, kwargs = seen[0]
    assert (method, url) == ("POST", "http://ap-mt:10014/api/runners/notion-token")
    assert kwargs["json"] == {"workspace": "ws-a", "token": "ntn_abc"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_delete_and_state_pass_the_workspace_as_a_query_param(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "ws-a")
    seen: list = []
    _stub_client(monkeypatch, FakeResponse(200, {"deleted": True}), seen)

    nts.delete(CONFIG)
    nts.state(CONFIG)
    assert [c[0] for c in seen] == ["DELETE", "GET"]
    assert all(c[2]["params"] == {"workspace": "ws-a"} for c in seen)


def test_no_platform_token_is_not_configured_not_a_failure():
    with pytest.raises(nts.NotionTokenNotConfigured):
        nts.push({}, "ntn_abc")


def test_an_http_error_becomes_a_sync_error(monkeypatch):
    _stub_client(monkeypatch, httpx.ConnectError("connection refused"))
    with pytest.raises(nts.NotionTokenSyncError, match="unreachable"):
        nts.push(CONFIG, "ntn_abc")


def test_a_503_from_ap_mt_is_surfaced_verbatim(monkeypatch):
    """That status specifically means AP-MT has no AGENTS_SECRET_KEY and
    refused to store the token in the clear — the operator has to see it."""
    _stub_client(monkeypatch, FakeResponse(503, None, "AGENTS_SECRET_KEY is not set"))
    with pytest.raises(nts.NotionTokenSyncError, match="AGENTS_SECRET_KEY"):
        nts.push(CONFIG, "ntn_abc")


# --- the reconcile leg (this app -> aw-app-notion) --------------------------


def test_reconcile_without_a_platform_token_does_nothing():
    assert nts.reconcile_once({}) == {
        "reconciled": False, "reason": "agents_platform_token not configured"}


def test_reconcile_reports_a_missing_notion_app_rather_than_failing(monkeypatch):
    monkeypatch.setattr(nts.observability_push_mod, "_local_api_key", lambda: "key")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            return FakeResponse(404, None, "Not Found")

    monkeypatch.setattr(nts.httpx, "Client", _Client)
    assert nts.reconcile_once(CONFIG) == {
        "reconciled": False, "reason": "aw-app-notion is not installed"}


def test_reconcile_forwards_the_notion_apps_verdict(monkeypatch):
    monkeypatch.setattr(nts.observability_push_mod, "_local_api_key", lambda: "key")
    seen: list = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return FakeResponse(200, {"reconciled": True, "changed": True, "action": "pushed"})

    monkeypatch.setattr(nts.httpx, "Client", _Client)
    result = nts.reconcile_once(CONFIG)

    assert result == {"reconciled": True, "changed": True, "action": "pushed"}
    url, kwargs = seen[0]
    assert url.endswith("/api/apps/notion/apmt/sync")
    assert kwargs["headers"] == {"X-Api-Key": "key"}


def test_reconcile_never_raises_on_a_dead_loopback(monkeypatch):
    """It runs inside a watchdog tick — see reconcile_once's docstring."""
    monkeypatch.setattr(nts.observability_push_mod, "_local_api_key", lambda: "key")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(nts.httpx, "Client", _Client)
    result = nts.reconcile_once(CONFIG)
    assert result["reconciled"] is False
    assert "connection refused" in result["reason"]


# --- routes -----------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(routes_mod.build_routes(dict(CONFIG)))


def test_route_push_requires_a_token(client):
    assert client.post("/notion-token", json={"token": "  "}).status_code == 400


def test_route_maps_not_configured_to_409_and_other_failures_to_502(client, monkeypatch):
    """aw-app-notion's logout branches on exactly this distinction: 409 means
    there is no remote copy (log out anyway), 502 means one may still be
    there (refuse to report success)."""
    def _not_configured(*a, **k):
        raise nts.NotionTokenNotConfigured("agents_platform_token is not configured")
    monkeypatch.setattr(routes_mod.notion_token_sync_mod, "delete", _not_configured)
    assert client.request("DELETE", "/notion-token").status_code == 409

    def _broken(*a, **k):
        raise nts.NotionTokenSyncError("AP-MT unreachable")
    monkeypatch.setattr(routes_mod.notion_token_sync_mod, "delete", _broken)
    assert client.request("DELETE", "/notion-token").status_code == 502


def test_route_state_returns_what_ap_mt_reported(client, monkeypatch):
    monkeypatch.setattr(routes_mod.notion_token_sync_mod, "state",
                        lambda cfg: {"configured": True, "token_fingerprint": "abc"})
    r = client.get("/notion-token/state")
    assert r.json() == {"configured": True, "token_fingerprint": "abc"}
