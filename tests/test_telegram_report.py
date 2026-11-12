"""Unit tests for telegram_report.py and the /telegram-report route (Kanban
feature:kanban-need-human-deterministic-reping) — the relay half: this app
owns the way to agents-platform-multitenant, the caller (aw-app-notion's
reping branch) owns nothing but the report content.

Same stub-the-transport style as test_notion_token_sync.py. No real network,
no AP-MT instance, no aw-app-notion.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agents_platform_runners_app import routes as routes_mod
from agents_platform_runners_app import telegram_report as tr

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
    """Replace httpx.Client inside telegram_report with one that records the
    request and answers `response` (or raises it, when it's an exception)."""
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
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(tr.httpx, "Client", _Client)


# --- the AP-MT leg ----------------------------------------------------------


def test_send_posts_title_text_and_bearer_token(monkeypatch):
    seen: list = []
    _stub_client(monkeypatch, FakeResponse(200, {"ok": True, "sent": 2}), seen)

    assert tr.send(CONFIG, "hi", "body") == {"ok": True, "sent": 2}
    url, kwargs = seen[0]
    assert url == "http://ap-mt:10014/api/telegram/report"
    assert kwargs["json"] == {"title": "hi", "text": "body"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_no_platform_token_is_not_configured_not_a_failure():
    with pytest.raises(tr.TelegramReportNotConfigured):
        tr.send({}, "hi", "body")


def test_an_http_error_becomes_a_sync_error(monkeypatch):
    _stub_client(monkeypatch, httpx.ConnectError("connection refused"))
    with pytest.raises(tr.TelegramReportSyncError, match="unreachable"):
        tr.send(CONFIG, "hi", "body")


def test_a_non_2xx_from_ap_mt_is_surfaced_verbatim(monkeypatch):
    _stub_client(monkeypatch, FakeResponse(503, None, "No sysadmin bot configured"))
    with pytest.raises(tr.TelegramReportSyncError, match="No sysadmin bot configured"):
        tr.send(CONFIG, "hi", "body")


# --- routes -----------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(routes_mod.build_routes(dict(CONFIG)))


def test_route_forwards_body_and_bearer(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(routes_mod.telegram_report_mod, "send",
                        lambda cfg, title, text: (
                            seen.append((cfg is not None, title, text)),
                            {"ok": True, "sent": 1})[1])

    r = client.post("/telegram-report", json={"title": "hi", "text": "body"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sent": 1}
    assert seen == [(True, "hi", "body")]


def test_route_maps_not_configured_to_409_and_other_failures_to_502(client, monkeypatch):
    def _not_configured(*a, **k):
        raise tr.TelegramReportNotConfigured("agents_platform_token is not configured")
    monkeypatch.setattr(routes_mod.telegram_report_mod, "send", _not_configured)
    assert client.post("/telegram-report", json={"title": "hi", "text": "x"}).status_code == 409

    def _broken(*a, **k):
        raise tr.TelegramReportSyncError("AP-MT refused the call (503): ...")
    monkeypatch.setattr(routes_mod.telegram_report_mod, "send", _broken)
    assert client.post("/telegram-report", json={"title": "hi", "text": "x"}).status_code == 502
