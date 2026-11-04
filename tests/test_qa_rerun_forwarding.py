"""`run_agent_async`'s `qa_rerun` passthrough.

AP-MT refuses a `qa-*` dispatch for a card/target that already has a QA run
(`core/executor.py::_check_qa_dispatch_dedup`, HTTP 409). `qa_rerun=true` is
the only override, which makes this one boolean the difference between "a
legitimate re-QA after rework runs" and "it is impossible to re-QA anything
through this tool".

The failure mode worth a test is a SILENT one: a `qa_rerun` the schema
accepts and the handler drops reaches the backend as a plain dispatch and
comes back 409, which reads to the caller as the dedup guard misbehaving
rather than as a dropped field. Exactly the shape of the `call_me_back_on`
drop this handler already carries a comment about.

Asserted on the outgoing request body, not on the response — the response is
whatever the mock returns, and what this layer owes AP-MT is the field.

Run: .venv/aw/bin/python -m pytest tests/test_qa_rerun_forwarding.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _patch_client(monkeypatch, recorder: list):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def _dispatch(monkeypatch, **extra) -> dict:
    recorder: list = []
    _patch_client(monkeypatch, recorder)
    asyncio.run(mcp_server._call_tool(
        "run_agent_async",
        {"slug": "qa-haiku", "target_slug": "some-target", "input": "review it", **extra},
    ))
    assert len(recorder) == 1
    return json.loads(recorder[0].content)


def test_qa_rerun_true_reaches_the_backend(monkeypatch):
    assert _dispatch(monkeypatch, qa_rerun=True)["qa_rerun"] is True


@pytest.mark.parametrize("extra", [{}, {"qa_rerun": False}])
def test_qa_rerun_is_omitted_unless_actually_set(monkeypatch, extra):
    """Sent only when true, so an ordinary dispatch keeps the body it always
    had — and so a `false` can never be mistaken for an explicit override by
    anything reading the request."""
    assert "qa_rerun" not in _dispatch(monkeypatch, **extra)


def test_the_schema_actually_declares_qa_rerun():
    """A handler that forwards a field no schema advertises is a field no
    agent will ever pass."""
    tools = asyncio.run(mcp_server._list_tools())
    (tool,) = [t for t in tools if t.name == "run_agent_async"]
    prop = tool.inputSchema["properties"]["qa_rerun"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    # The description has to say what a 409 means and when the override is
    # legitimate — this is the only place a dispatching agent sees either.
    assert "409" in tool.description
    assert "qa_rerun" in tool.description
    assert "409" in prop["description"] or "already exists" in prop["description"]
