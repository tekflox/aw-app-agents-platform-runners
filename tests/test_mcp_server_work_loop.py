"""``_work_loop_list_runs`` / ``_dispatch_manager_once`` — the two
``work_loop`` tool-handler helpers that talk to AP-MT over httpx. Scoped to
the code-review findings (PR #36) this file regression-tests: a 200
response with a malformed body used to raise uncaught out of functions
documented to "never raise" / always return a dict, escalating a single
candidate's (or the optional dispatch_manager hook's) failure into a
whole-loop/whole-call failure that discarded already-computed results.

Run: python -m pytest tests/test_mcp_server_work_loop.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server as ms  # noqa: E402


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_a_malformed_200_body_fails_closed_instead_of_raising():
    """Code-review finding, PR #36: ``r.json()`` was unguarded — a 200 with
    a non-JSON body used to raise uncaught, which run_work_loop's per-LOOP
    guard then caught, collapsing the WHOLE attention loop instead of
    failing just the one candidate whose probe hit it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async def _run():
        async with _client(handler) as c:
            return await ms._work_loop_list_runs(c, notion_task_id="p1")

    rows, error = asyncio.run(_run())
    assert rows is None
    assert error is not None and "malformed" in error


def test_a_well_formed_200_body_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "r1", "status": "success"}])

    async def _run():
        async with _client(handler) as c:
            return await ms._work_loop_list_runs(c, notion_task_id="p1")

    rows, error = asyncio.run(_run())
    assert error is None
    assert rows == [{"id": "r1", "status": "success"}]


def test_a_non_200_status_fails_closed_with_the_status_in_the_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async def _run():
        async with _client(handler) as c:
            return await ms._work_loop_list_runs(c, notion_task_id="p1")

    rows, error = asyncio.run(_run())
    assert rows is None
    assert error.startswith("503")


# ── _dispatch_manager_once ────────────────────────────────────────────────

def test_a_malformed_dedup_probe_body_fails_closed_instead_of_raising():
    """Code-review finding, PR #36: the dedup probe's ``probe.json()`` was
    unguarded, unlike ``_work_loop_list_runs`` in this same PR which was
    specifically hardened against exactly this. A malformed 200 here used
    to escape the whole ``work_loop`` call uncaught, discarding the
    already-computed work_orders/actions_taken/sections."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async def _run():
        async with _client(handler) as c:
            return await ms._dispatch_manager_once(c, [{"page_id": "p1"}])

    result = asyncio.run(_run())
    assert "error" in result and "malformed" in result["error"]


def test_a_malformed_dispatch_response_body_fails_closed_instead_of_raising():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[])  # dedup probe: no in-flight run
        return httpx.Response(200, text="not json")  # the dispatch POST itself

    async def _run():
        async with _client(handler) as c:
            return await ms._dispatch_manager_once(c, [{"page_id": "p1"}])

    result = asyncio.run(_run())
    assert "error" in result and "malformed" in result["error"]


def test_dispatch_is_skipped_when_a_manager_run_is_already_in_flight():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "r-live", "source_slug": "manager",
                                          "status": "running"}])

    async def _run():
        async with _client(handler) as c:
            return await ms._dispatch_manager_once(c, [{"page_id": "p1"}])

    result = asyncio.run(_run())
    assert result == {"skipped": "manager run already in flight", "run_id": "r-live"}


def test_work_order_line_handles_a_card_shaped_order():
    line = ms._work_order_line({"page_id": "p1", "reason": "stale",
                                "evidence": "ev", "suggested_action": "sa"})
    assert line == "page_id: p1\nreason: stale\nevidence: ev\nsuggested_action: sa"


def test_work_order_line_handles_a_pr_shaped_order():
    """The ``prs`` loop's shape (card 4175bf3b-9510-8163) has no `page_id`
    — `pr`/`url` identify it instead. Regression for the bug found live
    (card 4175bf3b-9510-8132): this used to be a bare `o['page_id']` lookup
    that raised ``KeyError`` on exactly this shape."""
    line = ms._work_order_line({"pr": "owner/repo#12", "url": "https://x/12",
                                "reason": "stalled", "evidence": "ev",
                                "suggested_action": "sa"})
    assert line == ("pr: owner/repo#12\nurl: https://x/12\n"
                    "reason: stalled\nevidence: ev\nsuggested_action: sa")


def test_dispatch_does_not_raise_on_a_pr_shaped_work_order():
    """End-to-end regression for the live crash: ``work_loop(loops=["prs"],
    dispatch_manager=true)`` threw ``KeyError: 'page_id'`` because this
    function assumed every work order was card-shaped. A PR-shaped order
    (no `page_id`) must flow through to a dispatched Manager run instead of
    raising."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[])  # dedup probe: nothing in flight
        assert b"pr: owner/repo#12" in request.content
        assert b"url: https://x/12" in request.content
        return httpx.Response(200, json={"run_id": "r-new"})

    async def _run():
        async with _client(handler) as c:
            return await ms._dispatch_manager_once(
                c, [{"pr": "owner/repo#12", "url": "https://x/12",
                     "reason": "stalled", "evidence": "ev", "suggested_action": "sa"}])

    result = asyncio.run(_run())
    assert result == {"run_id": "r-new"}


# ── work_loop tool handler: prs loop wiring ──────────────────────────────

# ── _prs_scan_via_route — the prs loop's real scanner (card 4175bf3b ──────
# gateway-boundary follow-up: mcp_server.py is a stdio child of the
# mcp-gateway's OWN container, which mounts neither /opt/aw-workspace/repos
# nor AW_WORKSPACE_HOME nor any gh/git credential, so pr_scan.scan_prs
# cannot run in-process here any more — it has to go over the published
# workspace API to this app's own core-side /pr-scan route.

def test_prs_scan_via_route_returns_the_response_body_on_success(monkeypatch):
    monkeypatch.setenv(ms.kanban_dispatch.API_KEY_VAR, "test-key")
    fake_result = {"owners": ["tekflox"], "candidates": [{"pr": "tekflox/aw-app-kb#7"}],
                  "work_orders": [], "tagged": [], "repos_unmapped": [], "counts": {},
                  "truncated": False, "errors": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/apps/agents-platform-runners/pr-scan")
        assert request.headers["X-Api-Key"] == "test-key"
        return httpx.Response(200, json=fake_result)

    async def _run():
        async with _client(handler) as c:
            return await ms._prs_scan_via_route(c, now=123.0, dry_run=True, limit=5,
                                                stall_hours=6.0)

    result = asyncio.run(_run())
    assert result == fake_result


def test_prs_scan_via_route_raises_on_a_non_200_instead_of_returning_an_empty_dict(monkeypatch):
    """The exact finding this hardening exists for: a dict return here
    (even an error dict) would come back out of ``ctx.scanner``
    indistinguishable from a real, successful-but-empty scan. This must
    RAISE so ``run_work_loop``'s per-loop guard turns it into a real
    ``errors`` entry — never a silent ``owners: []`` success."""
    monkeypatch.setenv(ms.kanban_dispatch.API_KEY_VAR, "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="core unreachable")

    async def _run():
        async with _client(handler) as c:
            return await ms._prs_scan_via_route(c, now=123.0, dry_run=False, limit=10,
                                                stall_hours=6.0)

    try:
        asyncio.run(_run())
        assert False, "expected a RuntimeError"
    except RuntimeError as exc:
        assert "502" in str(exc)


def test_prs_scan_via_route_raises_on_a_transport_error(monkeypatch):
    """Same reasoning as the 502 case — a core that is simply unreachable
    must not look like a scan that ran and found nothing."""
    monkeypatch.setenv(ms.kanban_dispatch.API_KEY_VAR, "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def _run():
        async with _client(handler) as c:
            return await ms._prs_scan_via_route(c, now=123.0, dry_run=False, limit=10,
                                                stall_hours=6.0)

    try:
        asyncio.run(_run())
        assert False, "expected a RuntimeError"
    except RuntimeError as exc:
        assert "could not reach this workspace's own API" in str(exc)


def test_prs_scan_via_route_raises_when_the_api_key_is_unset(monkeypatch):
    monkeypatch.delenv(ms.kanban_dispatch.API_KEY_VAR, raising=False)
    # This test runs inside a real workspace container that genuinely has
    # AW_WORKSPACE_HOME/.env with a real key — the env-var check above
    # alone isn't enough to simulate "unset", since the real code falls
    # back to that file exactly like the live upstream does.
    monkeypatch.setattr(ms.kanban_dispatch, "_from_env_file", lambda name: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not even attempt the call without a key")

    async def _run():
        async with _client(handler) as c:
            return await ms._prs_scan_via_route(c, now=123.0, dry_run=False, limit=10,
                                                stall_hours=6.0)

    try:
        asyncio.run(_run())
        assert False, "expected a RuntimeError"
    except RuntimeError as exc:
        assert ms.kanban_dispatch.API_KEY_VAR in str(exc)


# ── work_loop tool handler: prs loop wiring ──────────────────────────────

def test_work_loop_tool_wires_a_scanner_that_calls_the_pr_scan_route(monkeypatch):
    """The handler must hand ``ctx.scanner`` a callable that forwards to
    ``_prs_scan_via_route`` — not ``pr_scan.scan_prs`` directly any more
    (that import is gone from this gateway-side module entirely, card
    4175bf3b's gateway-boundary follow-up). ``run_work_loop`` itself is
    replaced so this stays a pure wiring check that never hits the
    network."""
    assert not hasattr(ms, "pr_scan"), (
        "mcp_server.py must not import pr_scan any more — the scan runs "
        "core-side now, reached over HTTP via _prs_scan_via_route")
    captured = {}

    async def fake_run_work_loop(ctx, loops):
        captured["ctx"] = ctx
        return {"ok": True, "dry_run": ctx.dry_run, "work_orders": [], "actions_taken": [],
               "loops_skipped": [], "errors": []}

    monkeypatch.setattr(ms.work_loop_mod, "run_work_loop", fake_run_work_loop)

    route_calls = []

    async def fake_via_route(c, **kw):
        route_calls.append(kw)
        return {"owners": [], "candidates": [], "work_orders": [], "tagged": [],
               "repos_unmapped": [], "counts": {}, "truncated": False, "errors": []}

    monkeypatch.setattr(ms, "_prs_scan_via_route", fake_via_route)

    asyncio.run(ms._dispatch_tool("work_loop", {"loops": ["prs"], "dry_run": True, "limit": 5}))
    scanner = captured["ctx"].scanner
    asyncio.run(scanner(now=42.0, dry_run=True, limit=5, stall_hours=6.0))
    assert route_calls == [{"now": 42.0, "dry_run": True, "limit": 5, "stall_hours": 6.0}]


def test_dispatch_succeeds_with_a_well_formed_round_trip():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[])  # dedup probe: nothing in flight
        return httpx.Response(200, json={"run_id": "r-new"})

    async def _run():
        async with _client(handler) as c:
            return await ms._dispatch_manager_once(c, [{"page_id": "p1", "reason": "stale"}])

    result = asyncio.run(_run())
    assert result == {"run_id": "r-new"}
