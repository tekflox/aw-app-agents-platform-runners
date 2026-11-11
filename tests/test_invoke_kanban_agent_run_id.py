"""`invoke_kanban_agent`'s optional `run_id` selector.

Kanban 4155bf3b-9510-81bb-999d-f25a3993fad7: the default lookup (newest
run tagged `category="work"` for the card) routinely pins a qa-* review or a
narrow helper sub-dispatch instead of the session actually deciding the
card, because AP-MT's `run_agent_async` inherits the caller's
`notion_task_id` onto every sub-dispatch. `run_id` lets a caller (the
kanban-sweeper) name the exact run to resume instead of trusting "newest".

Three behaviours pinned here: an explicit `run_id` bypasses the newest-work
lookup entirely; a `run_id` belonging to a DIFFERENT card is rejected before
anything is resumed; an absent `run_id` is the untouched default path.

Run: .venv/aw/bin/python -m pytest tests/test_invoke_kanban_agent_run_id.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _patch_client(monkeypatch, handler):
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def _call(monkeypatch, handler, **args) -> dict:
    _patch_client(monkeypatch, handler)
    (out,) = asyncio.run(mcp_server._call_tool("invoke_kanban_agent", args))
    return json.loads(out.text)


def test_explicit_run_id_skips_the_newest_work_lookup(monkeypatch):
    """The whole point: a direct /api/runs/{run_id} GET feeds resume_payload,
    and /api/runs?notion_task_id=...&category=work (the default lookup) is
    never called at all."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/api/runs/run-42":
            return httpx.Response(200, json={
                "id": "run-42", "notion_task_id": "page-1",
                "source_slug": "coder-sonnet", "session_id": "sess-9",
                "category": "work",
            })
        if request.method == "GET" and request.url.path == "/api/runs":
            raise AssertionError("newest-work lookup must not run when run_id is given")
        if request.method == "POST" and request.url.path == "/api/agents/coder-sonnet/run":
            return httpx.Response(200, json={"run_id": "new-run"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    result = _call(monkeypatch, handler, page_id="page-1", message="keep going",
                    run_id="run-42")
    assert result["resumed_session"] == "sess-9"
    assert any(r.url.path == "/api/runs/run-42" for r in seen)


def test_run_id_from_a_different_card_is_rejected(monkeypatch):
    """A caller passing a run_id lifted from someone else's card must not be
    able to resume that session under THIS card's identity."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/run-42":
            return httpx.Response(200, json={
                "id": "run-42", "notion_task_id": "some-other-page",
                "source_slug": "coder-sonnet", "session_id": "sess-9",
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    result = _call(monkeypatch, handler, page_id="page-1", message="keep going",
                    run_id="run-42")
    assert result["error"] is True
    assert result["status"] == 400
    assert "not tied to card" in result["message"]


def test_run_id_still_goes_through_the_resumability_checks(monkeypatch):
    """A monitor/wrapper row named explicitly by run_id is refused exactly
    like one found by the default lookup — the explicit selector is not a
    way around resume_payload's own guard."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/run-42":
            return httpx.Response(200, json={
                "id": "run-42", "notion_task_id": "page-1",
                "source_slug": "monitor-shell", "session_id": "sess-9",
                "category": "monitor",
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    result = _call(monkeypatch, handler, page_id="page-1", message="keep going",
                    run_id="run-42")
    assert result["error"] is True
    assert result["status"] == 409
    assert "monitor" in result["message"]


def test_absent_run_id_keeps_the_newest_work_behavior(monkeypatch):
    """No regression: omitting run_id still uses the newest category="work"
    run for the card, same as before this selector existed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs":
            assert request.url.params["notion_task_id"] == "page-1"
            assert request.url.params["category"] == "work"
            return httpx.Response(200, json=[{
                "id": "run-newest", "notion_task_id": "page-1",
                "source_slug": "coder-sonnet", "session_id": "sess-newest",
                "category": "work",
            }])
        if request.method == "POST" and request.url.path == "/api/agents/coder-sonnet/run":
            return httpx.Response(200, json={"run_id": "new-run"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    result = _call(monkeypatch, handler, page_id="page-1", message="keep going")
    assert result["resumed_session"] == "sess-newest"


def test_the_schema_actually_declares_run_id():
    tools = asyncio.run(mcp_server._list_tools())
    (tool,) = [t for t in tools if t.name == "invoke_kanban_agent"]
    prop = tool.inputSchema["properties"]["run_id"]
    assert prop["type"] == "string"
    assert "run_id" not in tool.inputSchema.get("required", [])
