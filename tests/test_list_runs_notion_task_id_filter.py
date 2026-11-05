"""mcp_server._call_tool's list_runs dispatch: notion_task_id was never in
the tool's advertised schema and never forwarded to AP-MT's ``GET /api/runs``
— the backend's own filter (``Run.notion_task_id == notion_task_id``) works
fine, but the MCP tool silently ignored the arg. This is the exact lookup
the kanban-sweeper's terminal-run check needs (Kanban 40f5bf3b-9510-816f-
9d53-dfb1e868893b): "latest run dispatched for this card" has no other
reliable path — property/scan/fuzzy lookups on the card itself are dead.

Run: .venv/aw/bin/python -m pytest tests/test_list_runs_notion_task_id_filter.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _patch_client(monkeypatch, recorder: list):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def test_list_runs_forwards_notion_task_id(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("list_runs", {"notion_task_id": "40f5bf3b-9510-816f-9d53-dfb1e868893b"}))

    assert recorder[0]["notion_task_id"] == ["40f5bf3b-9510-816f-9d53-dfb1e868893b"]


def test_list_runs_omits_notion_task_id_when_absent(monkeypatch):
    """No regression the other way: no notion_task_id arg means the global
    recency-ordered listing, same as before this fix."""
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("list_runs", {}))

    assert "notion_task_id" not in recorder[0]
