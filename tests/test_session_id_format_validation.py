"""A run_id passed where a session_id was expected only failed after a full
async dispatch/run/callback cycle — expensive, and confusing because the
error ("run finished without consuming any tokens") gave no hint that the id
itself was the wrong shape.

Root cause, confirmed empirically against the installed claude CLI: a
session_id is supposed to be a UUID, but `run_agent_async`/`supervise`
forwarded it to agents-platform-multitenant verbatim, unvalidated. AP-MT's
own validator (`is_session_id_usable()` in
`agents-platform-multitenant/backend/app/core/tools/docker_agent.py`) missed
the case because Python's `uuid.UUID()` accepts a bare 32-char hex string
with no hyphens — exactly the shape of a `run_id`
(`cfb8073699ce423dbab3fa335d12b143`). By the time AP-MT discovered that, the
container had already started and `claude --session-id <hex32>` had already
been rejected by the CLI.

This suite locks the synchronous half of the fix: `run_agent_async` and
`supervise` must reject a malformed session_id with a 400 in the SAME call,
before any HTTP request reaches the backend.

Run: .venv/aw/bin/python -m pytest tests/test_session_id_format_validation.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402

RUN_ID_HEX32 = "cfb8073699ce423dbab3fa335d12b143"  # the actual incident value
V4 = str(uuid.uuid4())
V3 = str(uuid.uuid3(uuid.NAMESPACE_DNS, "example.com"))
V7 = "01a06b7f-2589-7860-951e-506117d86b10"  # a live Codex-minted session id


def _patch_client(monkeypatch, recorder: list, *, json_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json=json_body if json_body is not None else {"run_id": "r1", "target_id": "t1"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def _err_payload(result):
    return json.loads(result[0].text)


# --- run_agent_async ---------------------------------------------------

def test_run_agent_async_rejects_hex32_without_touching_backend(monkeypatch):
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    result = asyncio.run(mcp_server._call_tool(
        "run_agent_async",
        {"slug": "some-agent", "target_slug": "some-target", "input": "hi", "session_id": RUN_ID_HEX32},
    ))

    payload = _err_payload(result)
    assert payload["error"] is True
    assert payload["status"] == 400
    assert "run_id" in payload["message"]
    assert RUN_ID_HEX32 in payload["message"]
    assert recorder == [], "malformed session_id must not reach the backend"


@pytest.mark.parametrize("session_id", [V4, V3, V7])
def test_run_agent_async_accepts_any_uuid_version(monkeypatch, session_id):
    """No version/variant constraint — v4, v3 and v7 (Codex) all pass."""
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    result = asyncio.run(mcp_server._call_tool(
        "run_agent_async",
        {"slug": "some-agent", "target_slug": "some-target", "input": "hi", "session_id": session_id},
    ))

    payload = _err_payload(result)
    assert payload.get("error") is not True, payload
    assert len(recorder) == 1
    sent = json.loads(recorder[0].content)
    assert sent["session_id"] == session_id


def test_run_agent_async_without_session_id_still_works():
    """session_id is optional — omitting it must not become a validation error."""
    async def _run():
        import httpx as _httpx

        real_async_client = _httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            kwargs["transport"] = _httpx.MockTransport(
                lambda req: _httpx.Response(200, json={"run_id": "r1", "target_id": "t1"})
            )
            return real_async_client(*args, **kwargs)

        mcp_server.httpx.AsyncClient = fake_async_client
        try:
            return await mcp_server._call_tool(
                "run_agent_async", {"slug": "some-agent", "target_slug": "some-target", "input": "hi"}
            )
        finally:
            mcp_server.httpx.AsyncClient = real_async_client

    result = asyncio.run(_run())
    payload = _err_payload(result)
    assert payload.get("error") is not True, payload


# --- supervise -----------------------------------------------------------

def test_supervise_rejects_hex32_without_touching_backend(monkeypatch):
    monkeypatch.setenv("AW_RUN_ID", "own-run-1")
    recorder: list = []
    _patch_client(monkeypatch, recorder)

    result = asyncio.run(mcp_server._call_tool(
        "supervise", {"session_id": RUN_ID_HEX32},
    ))

    payload = _err_payload(result)
    assert payload["error"] is True
    assert payload["status"] == 400
    assert "run_id" in payload["message"]
    assert recorder == [], "malformed session_id must not reach the backend"


@pytest.mark.parametrize("session_id", [V4, V7])
def test_supervise_accepts_uuid_v4_and_v7(monkeypatch, session_id):
    monkeypatch.setenv("AW_RUN_ID", "own-run-1")
    recorder: list = []
    _patch_client(monkeypatch, recorder, json_body={"id": "sup-1"})

    result = asyncio.run(mcp_server._call_tool(
        "supervise", {"session_id": session_id},
    ))

    payload = _err_payload(result)
    assert payload.get("error") is not True, payload
    assert len(recorder) == 1
    sent = json.loads(recorder[0].content)
    assert sent["target_session_id"] == session_id
