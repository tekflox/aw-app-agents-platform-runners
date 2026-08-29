"""mcp_server._call_tool's list_runs dispatch (2026-08-29): session_id was
never in the tool's advertised schema and never forwarded to AP-MT's
``GET /api/runs`` — the backend's own filter (``Run.session_id == session_id``)
works fine, but the MCP tool silently ignored the arg and always returned the
GLOBAL recency-ordered list across every session. Found live: a caller
verifying a wakeup-chain redirect passed session_id expecting only that
session's runs back, and got runs from unrelated sessions mixed in.

Run: .venv/aw/bin/python -m pytest tests/test_list_runs_session_id_filter.py
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


def test_list_runs_forwards_session_id(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("list_runs", {"session_id": "session-A"}))

    assert recorder[0]["session_id"] == ["session-A"]


def test_list_runs_omits_session_id_when_absent(monkeypatch):
    """No regression the other way: no session_id arg means the global
    recency-ordered listing, same as before this fix."""
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("list_runs", {}))

    assert "session_id" not in recorder[0]
