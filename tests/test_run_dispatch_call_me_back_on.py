"""mcp_server._call_tool's run_agent_async / run_workflow_async dispatch
(2026-08-29): call_me_back_on was advertised in both tools' schemas but
silently dropped when building the REST body forwarded to AP-MT's
``/api/agents/{slug}/run`` and ``/api/workflows/{slug}/run`` — every
redirect request degraded to "wake my own session" (or, with no caller run
identity to fall back to, a flat 400 from AP-MT's own validation), which is
exactly backwards for a middle hop in an A->B->C chain trying to redirect
C's answer straight to A.

Run: .venv/aw/bin/python -m pytest tests/test_run_dispatch_call_me_back_on.py
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


def _patch_client(monkeypatch, recorder: list):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def test_run_agent_async_forwards_call_me_back_on(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agent_async", {
        "slug": "coder-sonnet",
        "input": "do the thing",
        "target_slug": "some-target",
        "call_me_back_on": "session-A",
    }))

    assert recorder[0]["call_me_back_on"] == "session-A"


def test_run_agent_async_omits_call_me_back_on_when_absent(monkeypatch):
    """No regression the other way: omitting the arg must not send an empty
    or null call_me_back_on that could itself confuse the backend's
    caller-vs-redirect precedence."""
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agent_async", {
        "slug": "coder-sonnet",
        "input": "do the thing",
        "target_slug": "some-target",
    }))

    assert "call_me_back_on" not in recorder[0]


def test_run_workflow_async_forwards_call_me_back_on(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_workflow_async", {
        "slug": "some-workflow",
        "input": "do the thing",
        "target_slug": "some-target",
        "call_me_back_on": "session-B",
    }))

    assert recorder[0]["call_me_back_on"] == "session-B"
