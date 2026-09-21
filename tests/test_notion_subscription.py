"""Unit tests for notion_subscription.py (Kanban "auto-criar sweep de Ready
cards + automatizar/verificar webhook do Notion") — the AP-MT mapping the
Notion webhook needs before its manual dashboard step 4 can work. Same
stub-the-transport style as test_notion_token_sync.py. No real network, no
AP-MT instance, no aw-app-notion.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agents_platform_runners_app import notion_subscription as ns
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


def _stub_client(monkeypatch, responder, recorder=None):
    """Replace httpx.Client inside notion_subscription with one whose
    .get/.post(url, **kwargs) is answered by responder(method, url, kwargs)
    — a FakeResponse, or an exception instance to raise."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _do(self, method, url, **kwargs):
            if recorder is not None:
                recorder.append((method, url, kwargs))
            result = responder(method, url, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

        def get(self, url, **kwargs):
            return self._do("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self._do("POST", url, **kwargs)

    monkeypatch.setattr(ns.httpx, "Client", _Client)


# --- state(): translate AP-MT's rows into none/pending/verified ------------


def test_state_with_no_rows_is_none(monkeypatch):
    _stub_client(monkeypatch, lambda method, url, kwargs: FakeResponse(200, []))
    assert ns.state(CONFIG) == {"state": "none"}


def test_state_reports_pending_then_verified(monkeypatch):
    row = {"subscription_id": "sub-1", "notion_workspace_id": "ws-1", "verified": False}
    _stub_client(monkeypatch, lambda method, url, kwargs: FakeResponse(200, [row]))
    result = ns.state(CONFIG)
    assert result["state"] == "pending"
    assert result["subscription_id"] == "sub-1"

    row["verified"] = True
    _stub_client(monkeypatch, lambda method, url, kwargs: FakeResponse(200, [row]))
    assert ns.state(CONFIG)["state"] == "verified"


def test_state_without_a_token_is_unknown_not_a_crash():
    result = ns.state({})
    assert result["state"] == "unknown"
    assert "agents_platform_token" in result["reason"]


def test_state_never_raises_on_a_dead_ap_mt(monkeypatch):
    _stub_client(monkeypatch,
                lambda method, url, kwargs: httpx.ConnectError("connection refused"))
    result = ns.state(CONFIG)
    assert result["state"] == "unknown"
    assert "unreachable" in result["reason"]


# --- register(): the two-hop lookup + upsert -------------------------------


def test_register_looks_up_the_bot_identity_then_upserts(monkeypatch):
    monkeypatch.setattr(ns.observability_push_mod, "_local_api_key", lambda: "wskey")
    seen = []

    def responder(method, url, kwargs):
        if url.endswith("/api/apps/notion/bot"):
            assert kwargs["headers"] == {"X-Api-Key": "wskey"}
            return FakeResponse(200, {"integration_id": "int-1", "workspace_id": "ws-1",
                                      "workspace_name": "AW Dev"})
        assert url.endswith("/api/runners/notion-subscription")
        return FakeResponse(200, {"subscription_id": "sub-1", "notion_workspace_id": "ws-1",
                                  "integration_id": "int-1", "status": "pending",
                                  "verified": False})

    _stub_client(monkeypatch, responder, seen)

    result = ns.register(CONFIG, "sub-1")

    assert result["status"] == "pending"
    methods_urls = [(m, u) for m, u, _ in seen]
    assert methods_urls[0][1].endswith("/api/apps/notion/bot")
    assert methods_urls[1] == ("POST", "http://ap-mt:10014/api/runners/notion-subscription")
    post_body = seen[1][2]["json"]
    assert post_body == {"subscription_id": "sub-1", "notion_workspace_id": "ws-1",
                         "integration_id": "int-1"}
    assert seen[1][2]["headers"]["Authorization"] == "Bearer tok"


def test_register_requires_a_subscription_id():
    with pytest.raises(ns.NotionSubscriptionError, match="subscription_id"):
        ns.register(CONFIG, "  ")


def test_register_without_a_local_api_key_fails_before_any_network(monkeypatch):
    monkeypatch.setattr(ns.observability_push_mod, "_local_api_key", lambda: None)
    seen = []
    _stub_client(monkeypatch, lambda m, u, k: FakeResponse(200, {}), seen)

    with pytest.raises(ns.NotionSubscriptionError, match="AW_WORKSPACE_API_KEY"):
        ns.register(CONFIG, "sub-1")
    assert seen == []


def test_register_fails_when_aw_app_notion_has_no_token(monkeypatch):
    monkeypatch.setattr(ns.observability_push_mod, "_local_api_key", lambda: "wskey")
    _stub_client(monkeypatch, lambda m, u, k: FakeResponse(409, None, "no Notion token saved"))

    with pytest.raises(ns.NotionSubscriptionError, match="no Notion token configured"):
        ns.register(CONFIG, "sub-1")


def test_register_fails_when_bot_identity_carries_no_workspace_id(monkeypatch):
    monkeypatch.setattr(ns.observability_push_mod, "_local_api_key", lambda: "wskey")
    _stub_client(monkeypatch, lambda m, u, k: FakeResponse(
        200, {"integration_id": "int-1", "workspace_id": ""}))

    with pytest.raises(ns.NotionSubscriptionError, match="workspace_id"):
        ns.register(CONFIG, "sub-1")


def test_register_without_a_platform_token_fails_after_the_bot_lookup(monkeypatch):
    """Confirms the bot lookup happens first — a caller with aw-app-notion
    already configured has to see THIS app's own missing-token error, not a
    confusing "no Notion token" one that isn't actually the problem."""
    monkeypatch.setattr(ns.observability_push_mod, "_local_api_key", lambda: "wskey")
    _stub_client(monkeypatch, lambda m, u, k: FakeResponse(
        200, {"integration_id": "int-1", "workspace_id": "ws-1"}))

    with pytest.raises(ns.NotionSubscriptionError, match="agents_platform_token"):
        ns.register({}, "sub-1")


# --- routes -----------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(routes_mod.build_routes(dict(CONFIG)))


def test_route_requires_a_subscription_id(client):
    assert client.post("/notion-subscription", json={}).status_code == 400


def test_route_maps_notion_subscription_error_to_502(client, monkeypatch):
    def _boom(cfg, subscription_id):
        raise ns.NotionSubscriptionError("agents-platform-multitenant unreachable")
    monkeypatch.setattr(routes_mod.notion_subscription_mod, "register", _boom)
    resp = client.post("/notion-subscription", json={"subscription_id": "sub-1"})
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]


def test_route_returns_the_upserted_row(client, monkeypatch):
    monkeypatch.setattr(
        routes_mod.notion_subscription_mod, "register",
        lambda cfg, subscription_id: {"subscription_id": subscription_id, "status": "pending"})
    resp = client.post("/notion-subscription", json={"subscription_id": "sub-9"})
    assert resp.status_code == 200
    assert resp.json() == {"subscription_id": "sub-9", "status": "pending"}


def test_status_includes_kanban_sweep_and_notion_webhook_blocks(monkeypatch):
    monkeypatch.setattr(routes_mod.notion_subscription_mod, "state",
                        lambda cfg: {"state": "none"})
    status_client = TestClient(routes_mod.build_routes(
        {"kanban_sweep_enabled": True, "kanban_sweep_interval_s": 30},
        kanban_sweep_status={"watchdog_registered": True, "reason": None}))
    body = status_client.get("/status").json()
    assert body["kanban_sweep"] == {"enabled": True, "interval_s": 30,
                                    "watchdog_registered": True, "reason": None}
    assert body["notion_webhook"] == {"state": "none"}
