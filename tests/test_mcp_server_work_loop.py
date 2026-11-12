"""``_work_loop_list_runs`` — the ``ListRunsFn`` work_loop.py's attention
loop calls for its run-check. Scoped to the one code-review finding (PR #36)
this file exists to regression-test: a 200 response with a malformed body
used to raise uncaught out of this "never raises" helper, escalating a
single candidate's probe failure into a whole-loop failure.

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
