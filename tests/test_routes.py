"""TestClient coverage for agents_platform_runners_app/routes.py's
build_routes() (ADR Decision 6 item 6, docs/knowledge_base/docs/
architecture/adr-app-front-back-routes-dual-mode.md).

Run: .venv/aw/bin/python -m pytest tests/test_routes.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes, RUNNERS  # noqa: E402


def test_status_reports_every_runner():
    client = TestClient(build_routes())
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["runners"].keys()) == set(RUNNERS)
    for info in body["runners"].values():
        assert "installed" in info and "path" in info and "version" in info


def test_status_reflects_config():
    client = TestClient(build_routes({"agents_platform_base": "http://example.test:9999"}))
    resp = client.get("/status")
    assert resp.json()["agents_platform_base"] == "http://example.test:9999"


def test_warm_containers_without_a_container_socket_is_a_clear_error(monkeypatch):
    """No AW_CONTAINER_SOCKET must read as 'no engine available', never as an
    empty containers list — the same distinction /execute already makes."""
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", None)
    client = TestClient(build_routes())
    resp = client.get("/warm-containers")
    assert resp.status_code == 503
    assert "AW_CONTAINER_SOCKET is not set" in resp.json()["detail"]


# --------------------------------------------------------------------------
# AP-MT proxies (PLAN.md §2) — the path aw-app-crispal reaches
# agents-platform-multitenant by once it stops holding AP-MT credentials.
# --------------------------------------------------------------------------

AP_CFG = {"agents_platform_base": "http://ap-mt.test",
          "agents_platform_token": "id-jwt-123"}


def _ap(monkeypatch, handler, cfg=None) -> TestClient:
    """A client whose outbound AP-MT calls land on `handler` instead of the
    network. Patches the AsyncClient constructor rather than the module so the
    real httpx request/response semantics (headers, multipart encoding,
    raised HTTPError) are what the route actually sees."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return TestClient(build_routes(AP_CFG if cfg is None else cfg))


def test_gallery_upload_proxies_the_multipart_body_and_this_apps_token(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"block_id": "b1", "image_count": 1,
                                         "images": [{"id": "i1", "direct_url": "http://x/1"}]})

    client = _ap(monkeypatch, handler)
    resp = client.post("/gallery/upload", data={"bot_slug": "aw-cris", "source": "arvin"},
                       files=[("files", ("post.png", b"\x89PNG", "image/png"))])
    assert resp.status_code == 200
    assert resp.json()["block_id"] == "b1"
    sent = seen[0]
    assert str(sent.url) == "http://ap-mt.test/api/admin/gallery/upload"
    # The credential the caller does NOT have to hold any more.
    assert sent.headers["authorization"] == "Bearer id-jwt-123"
    body = sent.content
    assert b'name="bot_slug"' in body and b"aw-cris" in body
    assert b'name="source"' in body and b"arvin" in body
    assert b'filename="post.png"' in body and b"\x89PNG" in body


def test_gallery_upload_propagates_an_ap_mt_failure_instead_of_swallowing_it(monkeypatch):
    """The caller is a queue worker that has to tell "the gallery refused
    this" from "it worked". Answering 200 on a failed upload is what let an
    already-successful Arvin job be replayed for 5 minutes (PLAN.md §8)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom in the gallery")

    client = _ap(monkeypatch, handler)
    resp = client.post("/gallery/upload", data={"bot_slug": "aw-cris"},
                       files=[("files", ("a.png", b"x", "image/png"))])
    assert resp.status_code == 500
    assert "boom in the gallery" in resp.json()["detail"]


def test_an_unreachable_ap_mt_is_502(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = _ap(monkeypatch, handler)
    resp = client.post("/gallery/upload", data={"bot_slug": "aw-cris"},
                       files=[("files", ("a.png", b"x", "image/png"))])
    assert resp.status_code == 502
    assert "could not reach agents-platform" in resp.json()["detail"]


def test_run_initiator_proxies_the_telegram_route_not_the_identity_gated_one(monkeypatch):
    """`/api/runs/{id}` is behind a person's identity gate — reaching for it
    with a service credential 401'd in silence for 13 finished Arvin cycles."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"run_id": "r1", "initiator_kind": "telegram",
                                         "initiator_id": "chat-9"})

    client = _ap(monkeypatch, handler)
    resp = client.get("/runs/r1/initiator")
    assert resp.status_code == 200
    assert resp.json()["initiator_id"] == "chat-9"
    assert str(seen[0].url) == "http://ap-mt.test/api/telegram/run-initiator/r1"
    assert seen[0].headers["authorization"] == "Bearer id-jwt-123"


def test_run_initiator_keeps_ap_mts_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="run not found")

    client = _ap(monkeypatch, handler)
    assert client.get("/runs/nope/initiator").status_code == 404


def test_telegram_inject_passes_the_body_through_untouched(monkeypatch):
    """AP-MT owns /inject's schema. A proxy that reshapes the body becomes a
    second thing to keep in sync with it."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _ap(monkeypatch, handler)
    body = {"bot_id": "aw-cris", "chat_id": "42", "text": "a sua imagem está pronta",
            "source": "arvin"}
    resp = client.post("/telegram/inject", json=body)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert str(seen[0].url) == "http://ap-mt.test/api/telegram/inject"
    assert json.loads(seen[0].content) == body


def test_every_ap_proxy_says_503_when_this_app_has_no_token(monkeypatch):
    """Not 401: the caller's own credential was fine — it is THIS app that has
    nothing to present onward, and the fix is on this side."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call AP-MT with no token")

    client = _ap(monkeypatch, handler, cfg={"agents_platform_base": "http://ap-mt.test"})
    calls = [
        client.post("/gallery/upload", data={"bot_slug": "aw-cris"},
                    files=[("files", ("a.png", b"x", "image/png"))]),
        client.get("/runs/r1/initiator"),
        client.post("/telegram/inject", json={"text": "hi"}),
    ]
    for resp in calls:
        assert resp.status_code == 503
        assert "agents_platform_token" in resp.json()["detail"]
