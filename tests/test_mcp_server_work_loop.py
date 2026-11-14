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


def test_work_loop_tool_no_longer_wires_a_scanner():
    """The ``prs`` loop (and its real ``WorkLoopContext.scanner``,
    ``_prs_scan_via_route``) was retired in card 4185bf3b-9510-81a7 —
    superseded by aw-app-git's github-maintenance gate + Plan. Neither name
    should exist on this module any more."""
    assert not hasattr(ms, "pr_scan")
    assert not hasattr(ms, "_prs_scan_via_route")


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
