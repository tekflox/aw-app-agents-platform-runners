"""`_async_main`'s concurrent dispatch (card 4175bf3b-9510-811f).

Before this PR, `_async_main` read one line off stdin, `await`ed
`handle_request(request)` to completion, wrote the reply, and only THEN read
the next line — strictly sequential for the whole process's life. Confirmed
live on 2026-11-13: a `run_monitor_sync` call that legitimately blocked for
997.9s monopolized this one stdio child (shared by every caller on the
workspace) the entire time; two unrelated `mark_flow_done` calls queued
behind it with zero error/log/trace and eventually timed out client-side.

The fix dispatches every request as its own `asyncio.Task` (bounded by a
semaphore) instead of awaiting it inline, with a single dedicated writer
task owning `sys.stdout` so concurrent responses can never interleave
mid-line. These tests pin both halves of that fix plus the OTel contextvar
isolation the design relies on:

(a) a fast `tools/call` finishes (and its response is written) while a slow
    one is still in flight — proves dispatch is no longer end-to-end
    serialized.
(b) stdout under concurrent large responses — every captured line is one
    complete, valid, unfragmented/unmerged JSON object.
(c) `mcp_otel.tool_span`'s span-context isolation across concurrently
    running tasks — pins the asyncio invariant (a `contextvars.ContextVar`
    is copied per-`Task`, so one task's span never becomes "current" inside
    a sibling task) that makes this safe without any change to mcp_otel.py.
(d) a malformed `tools/call` (non-dict `params`/`arguments`) gets a real
    JSON-RPC error response instead of silently dying before `handle_request`
    is even reached — code-review finding, PR #50: the `tool_name`/
    `caller_run_id` extraction used to sit OUTSIDE `_handle_one`'s try/except.
(e) a `sys.stdout.write` failure inside the dedicated writer task crashes
    `_async_main` (so aw-mcp-gateway respawns a fresh child) instead of
    being swallowed while `write_queue` grows forever with nothing draining
    it — the other code-review finding, PR #50.

Run: python3 -m pytest tests/test_mcp_server_concurrent_dispatch.py
(or, per this repo's test-venv skill: /usr/bin/python3.12 -m pytest ...)
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server as ms  # noqa: E402


def _tool_call(req_id: int, name: str, arguments: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


class _FakeStdin:
    """Mimics `sys.stdin.readline()` — one line at a time, `""` on EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._it = iter(lines)

    def readline(self) -> str:
        try:
            return next(self._it) + "\n"
        except StopIteration:
            return ""


class _FakeStdout:
    """Captures every `write()` call exactly as `_async_main`'s writer task
    makes it — one call per completed response line, since that task is
    the only thing that ever touches this object."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, s: str) -> None:
        self.writes.append(s)

    def flush(self) -> None:
        pass


async def _drive(monkeypatch, lines: list[str]) -> _FakeStdout:
    fake_in = _FakeStdin(lines)
    fake_out = _FakeStdout()
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", fake_out)
    await ms._async_main()
    return fake_out


# ── (a) fast call is not stuck behind a slow one ─────────────────────────

def test_fast_call_completes_while_a_slow_call_is_still_in_flight(monkeypatch):
    completion_order: list[str] = []

    async def fake_dispatch_tool(name, arguments):
        if name == "slow":
            await asyncio.sleep(0.3)
        else:
            await asyncio.sleep(0.01)
        completion_order.append(name)
        return [ms.TextContent(type="text", text=f"{name}-done")]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    lines = [json.dumps(_tool_call(1, "slow")), json.dumps(_tool_call(2, "fast"))]
    out = asyncio.run(_drive(monkeypatch, lines))

    # The fast call must finish — and have its response WRITTEN — before the
    # slow one, proving the two are not serialized end-to-end.
    assert completion_order == ["fast", "slow"]
    responses = [json.loads(w) for w in out.writes]
    assert [r["id"] for r in responses] == [2, 1]


# ── (b) stdout line integrity under concurrent large responses ──────────

def test_stdout_lines_stay_whole_and_unmerged_under_concurrent_large_payloads(monkeypatch):
    n = 10
    blob_size = 20_000  # comfortably past a typical 4096-byte PIPE_BUF

    async def fake_dispatch_tool(name, arguments):
        idx = arguments["idx"]
        # Stagger completion order so responses don't drain in request order.
        await asyncio.sleep(0.002 * ((n - idx) % 5))
        payload = json.dumps({"idx": idx, "blob": "x" * blob_size})
        return [ms.TextContent(type="text", text=payload)]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    lines = [json.dumps(_tool_call(i, "big", {"idx": i})) for i in range(n)]
    out = asyncio.run(_drive(monkeypatch, lines))

    assert len(out.writes) == n
    seen_ids = set()
    for raw in out.writes:
        # Exactly one line per write — not a fragment, not two merged lines.
        assert raw.count("\n") == 1 and raw.endswith("\n")
        response = json.loads(raw)
        content = json.loads(response["result"]["content"][0]["text"])
        seen_ids.add(response["id"])
        assert content["idx"] == response["id"]
        assert len(content["blob"]) == blob_size
    assert seen_ids == set(range(n))


# ── (c) OTel tool_span context isolation across concurrent tasks ────────

class _FakeSpan:
    def set_attribute(self, *_a, **_kw) -> None:
        pass


class _FakeSpanContext:
    """Stands in for the real OTel SDK's `start_as_current_span()` —
    enough to exercise `mcp_otel.tool_span`'s own `with cm as span:` shape,
    backed by a real `contextvars.ContextVar` so this test can tell whether
    two concurrently-running tasks' spans ever clobber each other."""

    def __init__(self, var: contextvars.ContextVar, label: str) -> None:
        self._var = var
        self._label = label
        self._token = None

    def __enter__(self) -> _FakeSpan:
        self._token = self._var.set(self._label)
        return _FakeSpan()

    def __exit__(self, *_exc) -> bool:
        self._var.reset(self._token)
        return False


class _FakeTracer:
    def __init__(self, var: contextvars.ContextVar) -> None:
        self._var = var

    def start_as_current_span(self, name: str) -> _FakeSpanContext:
        return _FakeSpanContext(self._var, name)


def test_tool_span_context_does_not_leak_across_concurrent_tasks(monkeypatch):
    current_span = contextvars.ContextVar("test_current_span", default=None)
    monkeypatch.setattr(ms.mcp_otel, "_tracer", _FakeTracer(current_span))

    seen: dict[str, str | None] = {}

    async def fake_dispatch_tool(name, arguments):
        # Yield to the loop mid-flight (another task's tool_span will be
        # "current" in THAT task while this one is suspended here) before
        # reading back what this task sees as its own current span.
        await asyncio.sleep(0.05)
        seen[name] = current_span.get()
        return [ms.TextContent(type="text", text="ok")]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    lines = [json.dumps(_tool_call(1, "alpha")), json.dumps(_tool_call(2, "beta"))]
    asyncio.run(_drive(monkeypatch, lines))

    assert seen["alpha"] == "mcp.tool/alpha"
    assert seen["beta"] == "mcp.tool/beta"


# ── (d) malformed tools/call shapes get an error reply, not a silent hang ──
# Code-review finding, PR #50: `tool_name`/`caller_run_id` extraction used to
# sit OUTSIDE `_handle_one`'s try/except (and, deeper in, `handle_request`'s
# own `params.get("name")` assumed `params` is always a dict too) — a
# malformed request died with no response ever written for its id, and the
# caller would hang exactly like the head-of-line-blocking bug this whole PR
# fixes, just scoped to one request instead of the whole process.

def test_tools_call_with_non_dict_params_gets_an_error_and_does_not_block_a_good_request(monkeypatch):
    calls = {"n": 0}

    async def fake_dispatch_tool(name, arguments):
        calls["n"] += 1
        return [ms.TextContent(type="text", text="ok")]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    bad_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": ["not", "a", "dict"]}
    lines = [json.dumps(bad_request), json.dumps(_tool_call(2, "ok"))]

    out = asyncio.run(_drive(monkeypatch, lines))

    responses = {json.loads(w)["id"]: json.loads(w) for w in out.writes}
    assert set(responses) == {1, 2}
    assert "error" in responses[1] and responses[1]["error"]["code"] == -32603
    assert "result" in responses[2]
    assert calls["n"] == 1  # only the well-formed request actually dispatched


def test_tools_call_with_non_dict_arguments_does_not_crash_before_dispatch(monkeypatch):
    async def fake_dispatch_tool(name, arguments):
        return [ms.TextContent(type="text", text="ok")]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    bad_request = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "whatever", "arguments": "not-a-dict-string"}}
    out = asyncio.run(_drive(monkeypatch, [json.dumps(bad_request)]))

    assert len(out.writes) == 1
    response = json.loads(out.writes[0])
    assert response["id"] == 9


# ── (e) a writer failure crashes the process instead of being swallowed ───
# The other code-review finding, PR #50: nothing supervised the dedicated
# writer task. A `sys.stdout.write` failure (e.g. a broken pipe) used to die
# silently while the main loop kept accepting requests into a `write_queue`
# nothing drains — total silent response loss with no crash/restart signal.
# This must instead propagate out of `_async_main` so the process exits and
# aw-mcp-gateway respawns a healthy child.

class _FailingStdout:
    def write(self, _s: str) -> None:
        raise OSError("simulated broken pipe")

    def flush(self) -> None:
        pass


def test_a_broken_stdout_crashes_async_main_instead_of_being_swallowed(monkeypatch):
    async def fake_dispatch_tool(name, arguments):
        return [ms.TextContent(type="text", text="ok")]

    monkeypatch.setattr(ms, "_dispatch_tool", fake_dispatch_tool)

    fake_in = _FakeStdin([json.dumps(_tool_call(1, "x"))])
    monkeypatch.setattr(sys, "stdin", fake_in)
    monkeypatch.setattr(sys, "stdout", _FailingStdout())

    with pytest.raises(OSError, match="simulated broken pipe"):
        asyncio.run(ms._async_main())
