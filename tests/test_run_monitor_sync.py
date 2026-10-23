"""run_monitor_sync (2026-10-23, kanban 4025bf3b-9510-81af-adaa-c8b6bbab7caa):
a dispatched Coder/Architect ending its turn on a narrating "I'll wait in the
background" sentence with no tool call behind it is a compliance gap that
async dispatch (run_monitor_async) structurally cannot close on its own — the
gap sits between the dispatch call and whatever happens next turn.
run_monitor_sync blocks in the SAME tool call instead: dispatch via POST
/api/monitor/run (call_me_back=false), then the wait leg on GET
/api/runs/{run_id}/wait, using a DEDICATED long-timeout client — the shared
dispatch client used for every other tool in this file is timeout=120 and
would silently kill any wait beyond that regardless of the caller's own
timeout_s. That's the trap a test using the shared client wouldn't catch.

Also covers: agent_monitor_shell is retired, not just hidden. It never ran
anything — monitor-shell has no model_slug, so the generic agent-run path
this dynamic wrapper POSTs to resolves it to provider="echo", which
fabricates a nonzero-token "[echo] (no input)" fake success.

Run: .venv/aw/bin/python -m pytest tests/test_run_monitor_sync.py
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


def _patch_client(monkeypatch, recorder, *, wait_response=None, wait_raises=None):
    """Mock httpx.AsyncClient so the dispatch POST and the wait GET are both
    served by one handler, while recording every AsyncClient instantiation's
    `timeout=` kwarg — so tests can assert the wait leg used its own,
    dedicated (longer) timeout rather than the shared dispatch client's."""
    client_timeouts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        recorder.append({"method": request.method, "url": str(request.url), "body": body})
        if request.method == "POST" and request.url.path == "/api/monitor/run":
            return httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})
        if request.method == "GET" and request.url.path == "/api/runs/r1/wait":
            if wait_raises is not None:
                raise wait_raises
            return httpx.Response(200, json=wait_response or {"status": "success", "exit_code": 0})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        client_timeouts.append(kwargs.get("timeout"))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)
    return client_timeouts


def _result(contents):
    return json.loads(contents[0].text)


# --- dispatch forwarding ---------------------------------------------------

def test_run_monitor_sync_forwards_call_me_back_false(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
        "target_slug": "some-target",
    }))

    dispatch = recorder[0]["body"]
    assert dispatch["call_me_back"] is False
    assert dispatch["command"] == "echo hi"
    assert dispatch["target_slug"] == "some-target"


def test_run_monitor_sync_forwards_timeout_s_as_timeout_seconds(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
        "target_slug": "some-target",
        "timeout_s": 60,
    }))

    assert recorder[0]["body"]["timeout_seconds"] == 60
    wait_call = [r for r in recorder if r["method"] == "GET"][0]
    assert "timeout_s=60" in wait_call["url"]


def test_run_monitor_sync_defaults_timeout_s_to_600(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
        "target_slug": "some-target",
    }))

    assert recorder[0]["body"]["timeout_seconds"] == 600


def test_run_monitor_sync_caps_timeout_s_at_3600(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
        "target_slug": "some-target",
        "timeout_s": 999999,
    }))

    assert recorder[0]["body"]["timeout_seconds"] == 3600


def test_run_monitor_sync_requires_command(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "target_slug": "some-target",
    }))

    assert _result(out)["error"] is True
    assert recorder == []  # never dispatched


def test_run_monitor_sync_requires_target_slug(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
    }))

    assert _result(out)["error"] is True
    assert recorder == []


# --- the wait leg must use a dedicated, long-timeout client ----------------

def test_run_monitor_sync_wait_leg_uses_dedicated_long_timeout_client(monkeypatch):
    recorder: list = []
    client_timeouts = _patch_client(monkeypatch, recorder)

    asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "sleep 5; echo done; exit 3",
        "target_slug": "some-target",
        "timeout_s": 60,
    }))

    # First client = the shared dispatcher every tool call opens (timeout=120).
    # Second client = the dedicated wait-leg client, which must carry its OWN
    # budget (timeout_s + 30), not the shared one — that's the exact trap the
    # card named: a test reusing the shared client would pass here and still
    # die live on any wait longer than 120s.
    assert client_timeouts[0] == 120
    assert client_timeouts[1] == 60 + 30


def test_run_monitor_sync_returns_finished_snapshot(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder, wait_response={"status": "success", "exit_code": 3})

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "sleep 5; echo done; exit 3",
        "target_slug": "some-target",
        "timeout_s": 60,
    }))

    result = _result(out)
    assert result["status"] == "success"
    assert result["exit_code"] == 3
    assert "_sync_wait_note" not in result


# --- timing out is not success — and the run_id/recovery contract ----------

def test_run_monitor_sync_timeout_returns_running_snapshot_with_run_id(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder, wait_response={"status": "running"})

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "sleep 120",
        "target_slug": "some-target",
        "timeout_s": 10,
    }))

    result = _result(out)
    assert result.get("error") is not True
    assert result["status"] == "running"
    assert "_sync_wait_note" in result
    assert "r1" in result["_sync_wait_note"]
    assert "wait_run" in result["_sync_wait_note"]


def test_run_monitor_sync_wait_transport_error_carries_run_id_and_recovery(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder, wait_raises=httpx.ReadTimeout("boom"))

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "sleep 120",
        "target_slug": "some-target",
        "timeout_s": 10,
    }))

    result = _result(out)
    assert result["error"] is True
    assert result["run_id"] == "r1"
    assert "wait_run" in result["message"]
    assert "does NOT mean the command never ran" in result["message"]


def test_run_monitor_sync_wait_non_200_carries_run_id_and_recovery(monkeypatch):
    recorder: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        recorder.append({"method": request.method, "url": str(request.url), "body": body})
        if request.method == "POST" and request.url.path == "/api/monitor/run":
            return httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})
        return httpx.Response(500, text="boom")

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "sleep 120",
        "target_slug": "some-target",
        "timeout_s": 10,
    }))

    result = _result(out)
    assert result["error"] is True
    assert result["run_id"] == "r1"


def test_run_monitor_sync_dispatch_failure_has_no_run_id(monkeypatch):
    """If dispatch itself fails, there's no run to recover — the error must
    not fabricate a run_id that was never actually created."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad target")

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)

    out = asyncio.run(mcp_server._call_tool("run_monitor_sync", {
        "command": "echo hi",
        "target_slug": "some-target",
    }))

    result = _result(out)
    assert result["error"] is True
    assert "run_id" not in result


# --- monitor-shell is retired, not just hidden ------------------------------

def _patch_agents_workflows(monkeypatch, agents, workflows=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agents":
            return httpx.Response(200, json=agents)
        if request.url.path == "/api/workflows":
            return httpx.Response(200, json=workflows or [])
        raise AssertionError(f"unexpected request: {request.url}")

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def test_monitor_shell_absent_from_dynamic_tool_list(monkeypatch):
    mcp_server._LAST_GOOD_AGENTS = []
    mcp_server._LAST_GOOD_WORKFLOWS = []
    _patch_agents_workflows(monkeypatch, [
        {"slug": "monitor-shell", "name": "Monitor (shell)", "description": "x"},
        {"slug": "coder-sonnet", "name": "Coder", "description": "y"},
    ])

    tools = asyncio.run(mcp_server._list_tools())

    names = {t.name for t in tools}
    assert "agent_monitor_shell" not in names
    assert "agent_coder_sonnet" in names
    assert "run_monitor_sync" in names


def test_agent_monitor_shell_call_is_rejected(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    out = asyncio.run(mcp_server._call_tool("agent_monitor_shell", {
        "input": "ls",
        "target_slug": "some-target",
    }))

    result = _result(out)
    assert result["error"] is True
    assert "run_monitor_sync" in result["message"]
    assert recorder == []  # never actually dispatched to AP-MT
