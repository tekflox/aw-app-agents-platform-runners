"""mcp_server._call_tool's run_workflow_async / run_monitor_async /
run_agents_parallel dispatch (2026-09-09): call_me_back (and, for
run_agents_parallel, caller_run_id/call_me_back_on too) was never forwarded
to AP-MT at all here — unlike run_agent_async, nothing in these three
branches ever set body["call_me_back"], so AP-MT's RunInput.call_me_back
defaulted to False for every workflow/monitor/parallel dispatch regardless
of what the caller asked for. run_agents_parallel additionally forwarded
`args` wholesale, which never carries the resolved caller_run_id (the LLM
never sets one itself, and the raw `_gateway_caller_run_id` arg isn't the
field name AP-MT's _ParallelDispatch.caller_run_id expects) — so call_me_back
on a parallel fan-out had no origin_run_id to arm against either way.
Confirmed missing 2026-09-09, kanban 3d65bf3b-9510-817e-a98e-d700441f5d65.

Run: .venv/aw/bin/python -m pytest tests/test_run_dispatch_call_me_back_forwarding.py
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


# --- run_workflow_async -----------------------------------------------

def test_run_workflow_async_forwards_call_me_back_default_true(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_workflow_async", {
        "slug": "some-workflow",
        "input": "do the thing",
        "target_slug": "some-target",
    }))

    assert recorder[0]["call_me_back"] is True


def test_run_workflow_async_forwards_call_me_back_false(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_workflow_async", {
        "slug": "some-workflow",
        "input": "do the thing",
        "target_slug": "some-target",
        "call_me_back": False,
    }))

    assert recorder[0]["call_me_back"] is False


# --- run_monitor_async ---------------------------------------------------

def test_run_monitor_async_forwards_call_me_back_default_true(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_async", {
        "command": "echo hi",
        "target_slug": "some-target",
    }))

    assert recorder[0]["call_me_back"] is True


def test_run_monitor_async_forwards_call_me_back_false(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_async", {
        "command": "echo hi",
        "target_slug": "some-target",
        "call_me_back": False,
    }))

    assert recorder[0]["call_me_back"] is False


def test_run_monitor_async_forwards_call_me_back_on(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_async", {
        "command": "echo hi",
        "target_slug": "some-target",
        "call_me_back_on": "session-C",
    }))

    assert recorder[0]["call_me_back_on"] == "session-C"


# --- run_agents_parallel --------------------------------------------------

def test_run_agents_parallel_forwards_call_me_back_default_true(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
    }))

    assert recorder[0]["call_me_back"] is True


def test_run_agents_parallel_forwards_call_me_back_false(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
        "call_me_back": False,
    }))

    assert recorder[0]["call_me_back"] is False


def test_run_agents_parallel_forwards_call_me_back_on(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
        "call_me_back_on": "session-D",
    }))

    assert recorder[0]["call_me_back_on"] == "session-D"


def test_run_agents_parallel_forwards_resolved_caller_run_id(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)
    # Force the warm-container-file lookup (checked first by
    # _caller_run_id) to miss, so this test is deterministic regardless of
    # whether it happens to run inside a warm container itself.
    monkeypatch.setattr(mcp_server, "_WARM_CURRENT_RUN_ID_PATH", "/nonexistent/current_run_id")

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
        "_gateway_caller_run_id": "run-XYZ",
    }))

    assert recorder[0]["caller_run_id"] == "run-XYZ"


def test_run_agents_parallel_still_forwards_dispatches(monkeypatch):
    """No regression: the dict(args) rebuild must still carry every other
    field run_agents_parallel accepted before this fix."""
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
    }))

    assert recorder[0]["dispatches"] == [{"slug": "coder-sonnet", "input": "do it"}]
