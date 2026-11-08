"""The Plan MCP surface — Plan umbrella Slice 3
(AP-MT Kanban 4115bf3b-9510-8134-b9e6-e15db67189cd).

Four tools, three of them plain reads and one terminal. What this file
tests is the part that is this layer's own responsibility and nobody
else's: that each tool reaches the right AP-MT endpoint with the right
body, and that `finish_plan_iteration` fills in the caller's run id ITSELF.

That last one carries the only real risk here. The iteration a steward may
close is *its own*, and the run id that identifies it is gateway-injected
(`_caller_run_id`) rather than supplied by the model — so a handler that
forwarded a model-supplied run id, or forgot to forward one at all, would
either let a steward close another plan's iteration or produce a terminal
call that silently closes nothing. Both fail quietly: the agent reports a
decision, and the plan's claim stays held until the watchdog.

Asserted on the outgoing request, not on the response — the response is
whatever the mock returns, and what this layer owes AP-MT is the request.

Run: .venv/aw/bin/python -m pytest tests/test_plan_tools.py
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


def _patch_client(monkeypatch, recorder: list, *, status: int = 200,
                  payload: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(status, json=payload if payload is not None else {"ok": True})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def _call(monkeypatch, name: str, args: dict, *, status: int = 200,
          payload: dict | None = None) -> httpx.Request:
    recorder: list = []
    _patch_client(monkeypatch, recorder, status=status, payload=payload)
    asyncio.run(mcp_server._call_tool(name, args))
    assert len(recorder) == 1, f"{name} made {len(recorder)} HTTP calls, expected 1"
    return recorder[0]


@pytest.fixture(autouse=True)
def no_warm_turn_file(monkeypatch):
    """Point `_WARM_CURRENT_RUN_ID_PATH` at nothing.

    That file is source 1 in `_caller_run_id`'s precedence and it genuinely
    exists inside a warm agent container — including the one these tests run
    in — so without this every run-id assertion below reads whatever turn
    this session happens to be on. Same isolation
    `test_run_dispatch_call_me_back_forwarding.py` uses.
    """
    monkeypatch.setattr(mcp_server, "_WARM_CURRENT_RUN_ID_PATH",
                        "/nonexistent/current_run_id")


def _tool(name: str):
    tools = asyncio.run(mcp_server._list_tools())
    matches = [t for t in tools if t.name == name]
    assert len(matches) == 1, f"expected exactly one {name} tool, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "path"), [
    ("get_plan", "/api/plans/my-plan"),
    ("plan_summary", "/api/plans/my-plan/summary"),
    ("list_plan_iterations", "/api/plans/my-plan/iterations"),
])
def test_each_plan_read_reaches_its_own_endpoint(monkeypatch, name, path):
    req = _call(monkeypatch, name, {"slug": "my-plan"})
    assert req.method == "GET"
    assert req.url.path == path


def test_list_plan_iterations_forwards_limit(monkeypatch):
    req = _call(monkeypatch, "list_plan_iterations", {"slug": "my-plan", "limit": 5})
    assert req.url.params["limit"] == "5"


def test_list_targets_forwards_plan_slug(monkeypatch):
    """`list_targets(plan_slug=...)` — the read a steward needs before
    deciding whether to push an existing member Target or open another. A
    dropped filter here returns EVERY target in the tenant, which reads as a
    plan that has far more members than it does."""
    req = _call(monkeypatch, "list_targets", {"plan_slug": "my-plan"}, payload=[])
    assert req.url.path == "/api/targets"
    assert req.url.params["plan_slug"] == "my-plan"


def test_plan_slug_is_declared_on_list_targets(monkeypatch):
    """A handler that forwards a field no schema advertises is a field no
    agent will ever pass."""
    assert "plan_slug" in _tool("list_targets").inputSchema["properties"]


# ---------------------------------------------------------------------------
# finish_plan_iteration — the terminal call
# ---------------------------------------------------------------------------

def test_finish_sends_the_callers_own_run_id(monkeypatch):
    """The run id comes from `_caller_run_id`, not from the model.

    A steward that could name any run could close another plan's iteration;
    and since the server resolves the iteration from
    `runs.plan_iteration_id`, forwarding nothing at all produces a terminal
    call that closes nothing while reporting success.
    """
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    req = _call(monkeypatch, "finish_plan_iteration",
                {"decision": "pushed", "notes": "pushed the member target"})
    assert req.method == "POST"
    assert req.url.path == "/api/plans/finish-iteration"
    body = json.loads(req.content)
    assert body == {"decision": "pushed", "notes": "pushed the member target",
                    "run_id": "run-abc"}


def test_finish_prefers_an_explicit_iteration_id_over_the_run_id(monkeypatch):
    """Explicitly given, the iteration id wins — and the run id is then NOT
    also sent, so the server never has to break a tie between two different
    answers to "which iteration"."""
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    body = json.loads(_call(monkeypatch, "finish_plan_iteration",
                            {"decision": "noop", "plan_iteration_id": "it-1"}).content)
    assert body["plan_iteration_id"] == "it-1"
    assert "run_id" not in body


def test_finish_prefers_the_gateway_injected_run_id(monkeypatch):
    """`_gateway_caller_run_id` beats `AW_RUN_ID` — a warm container's env
    can carry a STALE run id from a previous run (confirmed live 2026-08-29,
    WS-8), so the per-call injection is the authoritative one."""
    monkeypatch.setenv("AW_RUN_ID", "stale-run")
    body = json.loads(_call(monkeypatch, "finish_plan_iteration",
                            {"decision": "noop",
                             "_gateway_caller_run_id": "live-run"}).content)
    assert body["run_id"] == "live-run"


def test_finish_refuses_rather_than_closing_nothing_when_no_run_id_exists(monkeypatch):
    """No caller run and no explicit iteration id: there is nothing that
    identifies an iteration, so this must be an error the agent SEES.

    The alternative — posting a body with neither id and letting the server
    422 — is the same outcome with a worse message, and posting with neither
    while reporting success is how a plan's claim stays held until the
    watchdog frees it two hours later.
    """
    monkeypatch.delenv("AW_RUN_ID", raising=False)
    recorder: list = []
    _patch_client(monkeypatch, recorder)
    out = asyncio.run(mcp_server._call_tool("finish_plan_iteration",
                                            {"decision": "noop"}))
    assert recorder == [], "it called the backend with nothing identifying an iteration"
    body = json.loads(out[0].text)
    assert body["status"] == 400
    assert "plan_iteration_id" in body["message"]


def test_the_decision_enum_matches_what_the_server_validates(monkeypatch):
    """The schema's enum and AP-MT's `core/plans.ITERATION_DECISIONS` have to
    agree. They are in two repos, so nothing but a test keeps them aligned —
    and the failure is a terminal call rejected with a 422 at the worst
    possible moment, which an agent reads as "my work was not recorded".

    `error` and `auto_conclude` are deliberately NOT offered: the first is
    what the executor backstop writes for an iteration that never closed
    itself, and the second is written by auto-conclusion's own audit row.
    Neither is a decision an agent announces.
    """
    enum = set(_tool("finish_plan_iteration").inputSchema["properties"]["decision"]["enum"])
    assert enum == {"pushed", "created_target", "evidence_recorded",
                    "proposed_conclude", "concluded", "noop", "blocked"}


def test_the_description_tells_the_agent_what_a_409_means():
    """The only place a steward learns that a 409 is the watchdog having
    taken its iteration over — and therefore not retryable. Without it the
    agent retries a refused close, or reports the refusal as a tool bug."""
    desc = _tool("finish_plan_iteration").description
    assert "409" in desc
    assert "not a retryable error" in desc
    assert "proposed_conclude" in desc


def test_only_the_read_and_terminal_plan_tools_exist(monkeypatch):
    """No `conclude_plan`, no `reopen_plan`, no force, no waive — at any
    point, now or later.

    "Force/waive/reopen remain human-authorized" is a product decision
    repeated on three cards, and the way it is kept is that no tool exposes
    them. This test is what makes adding one a deliberate act rather than an
    oversight: Slice 5's management surface will have to argue with it.
    """
    names = {t.name for t in asyncio.run(mcp_server._list_tools())}
    assert {"get_plan", "plan_summary", "list_plan_iterations",
            "finish_plan_iteration"} <= names
    assert not {"conclude_plan", "reopen_plan", "force_conclude_plan",
                "waive_plan_criterion"} & names
