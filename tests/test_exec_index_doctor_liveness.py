"""The doctor's service-liveness dimension, and the search proxy route.

docs/design/execution-index-migration-off-aw-app-kb.md §6 names this as the
delivery's own risk: ``src/apps/services.py`` respawns a service that
*crashed*, but "a service that was never started" is explicitly outside what
it detects (its docstring says the activation audit is not built, and there
is a live known gap around ``activate()`` being skipped after a core
restart). So the instruction was: ship a doctor entry that fails when the
producer is configured to write to the service but ``/status`` does not
answer — do not trust install-time green.
"""
from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execution_index as idx  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402

EXEC_URL = "http://exec-index.test:9408"


class _FakeRedis:
    """Enough of the sync client for counters_snapshot/_window_snapshot."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def mget(self, keys):
        return [self.data.get(k) for k in keys]

    def scan_iter(self, match=None):
        return [k for k in self.data if match is None or fnmatch.fnmatch(k, match)]


@pytest.fixture(autouse=True)
def _counters(monkeypatch):
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: _FakeRedis())


def _client(exec_status_response, *, url=EXEC_URL):
    """A routes app whose exec-index service answers ``exec_status_response``.

    The route builds its own ``httpx.AsyncClient``, so the transport is
    patched at the class level rather than injected.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if exec_status_response is None:
            raise httpx.ConnectError("Connection refused", request=request)
        return exec_status_response

    real_init = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    return init, TestClient(build_routes({"exec_index_url": url}))


def test_doctor_is_red_when_the_service_does_not_answer(monkeypatch):
    init, client = _client(None)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)

    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is False
    detail = body["detail"]["exec_index_service"]
    assert detail == {"configured": True, "reachable": False,
                      "error": detail["error"]}
    assert "ConnectError" in detail["error"]
    # The problem text has to name the address and say what the consequence
    # is — a doctor line that only says "not ok" sends someone reading logs.
    assert any(EXEC_URL in p and "exec_post write" in p
               for p in body["detail"]["problems"])


def test_doctor_is_green_when_the_service_answers_ok(monkeypatch):
    init, client = _client(httpx.Response(200, json={
        "ok": True, "chunk_count": 24848, "run_count": 2668}))
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)

    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is True
    assert body["detail"]["exec_index_service"]["reachable"] is True
    assert body["detail"]["exec_index_service"]["status"]["chunk_count"] == 24848


def test_a_live_service_with_an_unusable_store_is_still_red(monkeypatch):
    # The process being up is not the same as the index being writable: a
    # recreated shared Postgres with a lost volume answers /status fine and
    # reports its own error, which the design flags as a NEW failure mode
    # this migration introduces (§5, "infra the app framework doesn't
    # manage").
    init, client = _client(httpx.Response(200, json={
        "ok": False, "error": "could not connect to server", "chunk_count": 0}))
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)

    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is False
    assert any("store unusable" in p for p in body["detail"]["problems"])


def test_a_deliberately_blank_target_is_not_an_outage(monkeypatch):
    # Blanking exec_index_url IS the rollback of cutover step 2. A rollback
    # must not read as a broken service, or the doctor cries wolf for as long
    # as the rollback is in place.
    init, client = _client(None, url="")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)

    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is True
    assert body["detail"]["exec_index_service"] == {"configured": False}


def test_search_proxies_to_the_service_and_returns_raw_chunk_scores(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"total": 3, "results": [
            {"chunk_id": "r1:0", "run_id": "r1", "seq": 0, "content": "x",
             "metadata": {"status": "error"}, "score": 0.77},
        ]})

    real_init = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    client = TestClient(build_routes({"exec_index_url": EXEC_URL}))

    resp = client.post("/execution-index/search",
                       json={"query": "pg refused", "n_results": 2,
                             "filters": {"status": "error"}})
    assert resp.status_code == 200
    assert captured["url"] == f"{EXEC_URL}/search"
    assert '"status": "error"' in captured["body"] or '"status":"error"' in captured["body"]
    # Raw per-chunk scores, not rendered text: the parity check compares
    # scores, and the MCP handler does the grouping.
    assert resp.json()["results"][0]["score"] == 0.77


def test_search_reports_an_unreachable_service_as_502_not_as_no_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    real_init = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    client = TestClient(build_routes({"exec_index_url": EXEC_URL}),
                        raise_server_exceptions=False)
    resp = client.post("/execution-index/search", json={"query": "anything"})
    # An empty result set would read as "we have never seen this before",
    # which is the single most misleading answer this tool can give.
    assert resp.status_code == 502


def test_search_with_no_configured_target_is_503(monkeypatch):
    client = TestClient(build_routes({"exec_index_url": ""}),
                        raise_server_exceptions=False)
    resp = client.post("/execution-index/search", json={"query": "anything"})
    assert resp.status_code == 503
