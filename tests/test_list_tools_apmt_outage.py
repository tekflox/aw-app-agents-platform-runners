"""mcp_server._list_tools() (2026-08-31, resilience:runners-list-tools-fails-open-on-apmt-outage):
the two httpx calls to AP-MT (/api/agents, /api/workflows) were unguarded, so
any AP-MT hiccup made the ENTIRE ~149-tool namespace vanish from tools/list —
including mark_flow_done, run_agent_async, ask_human. Fixed by wrapping both
calls in try/except and falling back to the static tool set plus a cached
copy of the last-successful dynamic (agent_<slug>/workflow_<slug>) list, so
an AP-MT outage degrades to individually-stale dynamic tools instead of
wiping the namespace.

Run: .venv/aw/bin/python -m pytest tests/test_list_tools_apmt_outage.py
"""
from __future__ import annotations

import asyncio
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


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/agents":
        return httpx.Response(200, json=[{"slug": "coder-sonnet", "name": "Coder",
                                           "description": "d"}])
    if request.url.path == "/api/workflows":
        return httpx.Response(200, json=[{"slug": "review", "name": "Review",
                                           "kind": "pipeline", "description": "d"}])
    raise AssertionError(f"unexpected request: {request.url}")


def _down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_outage_falls_back_to_static_tools_only(monkeypatch):
    """No prior successful poll (cold start) + AP-MT down: tools/list must
    still return the static tool set, not raise / return empty."""
    mcp_server._LAST_GOOD_AGENTS = []
    mcp_server._LAST_GOOD_WORKFLOWS = []
    _patch_client(monkeypatch, _down_handler)

    tools = asyncio.run(mcp_server._list_tools())

    names = {t.name for t in tools}
    assert "list_agents" in names
    assert "run_agent_async" in names
    assert "mark_flow_done" in names
    assert "ask_human" in names
    assert not any(n.startswith("agent_") or n.startswith("workflow_") for n in names)


def test_outage_keeps_last_known_good_dynamic_tools(monkeypatch):
    """A prior successful poll cached agent_/workflow_ runners; AP-MT then
    goes down. Those dynamic tools must stay in tools/list (stale but
    present) instead of vanishing along with the whole namespace."""
    mcp_server._LAST_GOOD_AGENTS = []
    mcp_server._LAST_GOOD_WORKFLOWS = []
    _patch_client(monkeypatch, _ok_handler)
    tools = asyncio.run(mcp_server._list_tools())
    names = {t.name for t in tools}
    assert "agent_coder_sonnet" in names
    assert "workflow_review" in names

    _patch_client(monkeypatch, _down_handler)
    tools = asyncio.run(mcp_server._list_tools())
    names = {t.name for t in tools}

    assert "agent_coder_sonnet" in names
    assert "workflow_review" in names
    assert "list_agents" in names


def test_recovery_refreshes_the_cache(monkeypatch):
    """Once AP-MT comes back, a successful poll should replace the stale
    cache rather than just keep appending to it."""
    mcp_server._LAST_GOOD_AGENTS = [{"slug": "stale-agent", "name": "Stale",
                                      "description": "d"}]
    mcp_server._LAST_GOOD_WORKFLOWS = []
    _patch_client(monkeypatch, _ok_handler)

    tools = asyncio.run(mcp_server._list_tools())

    names = {t.name for t in tools}
    assert "agent_coder_sonnet" in names
    assert "agent_stale_agent" not in names
