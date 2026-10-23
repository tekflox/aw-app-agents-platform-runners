"""mcp_server._own_workspace() (docs/design/ap-mt-workspace-cascade.md,
slice 2): every dispatch handler that posts a new Run to AP-MT names this
workspace explicitly — tier 1 of AP-MT's resolver, never inference. Server-
side only, like `_caller_run_id` (see that function's docstring) — never
exposed on any LLM-facing `inputSchema`, so these tests never pass
`workspace` in `args` themselves; that would prove the wrong thing (the key
surviving a pass-through) rather than the real one (this process injecting
it unprompted).

Same `_patch_client` pattern as test_run_dispatch_call_me_back_forwarding.py
— intercepts the outbound POST and records its JSON body, so these tests
exercise mcp_server.py's own forwarding logic in isolation from AP-MT.

Run: .venv/aw/bin/python -m pytest tests/test_run_dispatch_workspace.py
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


def test_own_workspace_reads_the_published_env_var(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "crispal")
    assert mcp_server._own_workspace() == "crispal"


def test_own_workspace_defaults_to_aw(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setattr(mcp_server.platform_base, "workspace_env", lambda name: "")
    assert mcp_server._own_workspace() == "aw"


# --- every dispatch handler carries workspace in its POST body -----------

def test_run_agent_async_carries_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agent_async", {
        "slug": "coder-sonnet", "input": "do it", "target_slug": "some-target",
    }))

    assert recorder[0]["workspace"] == "aw"


def test_run_workflow_async_carries_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_workflow_async", {
        "slug": "some-workflow", "input": "do it", "target_slug": "some-target",
    }))

    assert recorder[0]["workspace"] == "aw"


def test_run_monitor_async_carries_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_async", {
        "command": "echo hi", "target_slug": "some-target",
    }))

    assert recorder[0]["workspace"] == "aw"


def test_run_monitor_sync_carries_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        recorder.append(body)
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json={"status": "success", "run_id": "r1"})
        return httpx.Response(200, json={"run_id": "r1"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi", "target_slug": "some-target",
    }))

    assert recorder[0]["workspace"] == "aw"


def test_run_agents_parallel_carries_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
    }))

    assert recorder[0]["workspace"] == "aw"


# --- setdefault never clobbers an explicit value --------------------------

def test_run_agents_parallel_does_not_clobber_an_already_set_workspace(monkeypatch):
    """`run_agents_parallel` is the one handler that forwards `args` wholesale
    (`body = dict(args)`) — not reachable via the real inputSchema (workspace
    isn't a declared property), but a defensive check that the injection is
    setdefault, not an overwrite, exactly §5's "injection must therefore use
    setdefault, never clobber" constraint, in case a future caller's args
    already carry one."""
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_agents_parallel", {
        "target_slug": "some-target",
        "dispatches": [{"slug": "coder-sonnet", "input": "do it"}],
        "workspace": "crispal",
    }))

    assert recorder[0]["workspace"] == "crispal"
