"""`create_target` / `update_target` plan-claim surface
(AP-MT Kanban 4155bf3b-9510-81b4-b925-f72e987a27c6, half 2 of 2 — the backend
half lives in agents-platform-multitenant's test_plans_api.py).

A Target created mid-run by an agent working under a Plan should be claimed
by that Plan: explicitly via `plan_slug`, or automatically from the caller's
own run context. The explicit half is a plain schema+forward — the risk is
all in the automatic half: `claimed_by_run_id` identifies the CALLING run,
and like `finish_plan_iteration`'s `run_id` (see test_plan_tools.py) it must
come from `_caller_run_id`, never from the model. A handler that let the
model supply it could claim a Target under a Plan the model merely knows the
slug of (not one it is actually working under); a handler that forgot to
send it at all would make every Target created inside a Plan's run tree
silently unclaimed, since the backend's auto-claim path only fires when
`claimed_by_run_id` is present.

Run: .venv/aw/bin/python -m pytest tests/test_target_plan_claim_tools.py
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
    """Same isolation as test_plan_tools.py — without this, `_caller_run_id`
    reads whatever turn file genuinely exists in this warm container."""
    monkeypatch.setattr(mcp_server, "_WARM_CURRENT_RUN_ID_PATH",
                        "/nonexistent/current_run_id")


def _tool(name: str):
    tools = asyncio.run(mcp_server._list_tools())
    matches = [t for t in tools if t.name == name]
    assert len(matches) == 1, f"expected exactly one {name} tool, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Schema — plan_slug has to be declared, or no agent will ever pass it.
# ---------------------------------------------------------------------------

def test_plan_slug_is_declared_on_create_target(monkeypatch):
    assert "plan_slug" in _tool("create_target").inputSchema["properties"]


def test_plan_slug_is_declared_on_update_target(monkeypatch):
    assert "plan_slug" in _tool("update_target").inputSchema["properties"]


# ---------------------------------------------------------------------------
# create_target — explicit plan_slug is a plain forward.
# ---------------------------------------------------------------------------

def test_create_target_forwards_an_explicit_plan_slug(monkeypatch):
    req = _call(monkeypatch, "create_target",
               {"slug": "us1924311", "name": "t", "plan_slug": "the-plan"})
    assert req.method == "POST"
    assert req.url.path == "/api/targets"
    body = json.loads(req.content)
    assert body["plan_slug"] == "the-plan"


# ---------------------------------------------------------------------------
# create_target — claimed_by_run_id is INJECTED from the caller's own run,
# never taken from the model, mirroring finish_plan_iteration's run_id.
# ---------------------------------------------------------------------------

def test_create_target_injects_the_callers_own_run_id_as_claimed_by_run_id(monkeypatch):
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t"}).content)
    assert body["claimed_by_run_id"] == "run-abc"


def test_create_target_prefers_the_gateway_injected_run_id_for_claim(monkeypatch):
    """Same precedence as `_caller_run_id` everywhere else: a warm
    container's env can carry a stale run id, so the per-call gateway
    injection wins."""
    monkeypatch.setenv("AW_RUN_ID", "stale-run")
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t",
                             "_gateway_caller_run_id": "live-run"}).content)
    assert body["claimed_by_run_id"] == "live-run"


def test_create_target_ignores_a_model_supplied_claimed_by_run_id(monkeypatch):
    """The model cannot name an arbitrary run to claim a Target under —
    whatever it puts in `claimed_by_run_id` is overwritten with the real
    caller run id (or dropped, if none is available)."""
    monkeypatch.setenv("AW_RUN_ID", "run-real")
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t",
                             "claimed_by_run_id": "someone-elses-run"}).content)
    assert body["claimed_by_run_id"] == "run-real"


def test_create_target_sends_a_null_claimed_by_run_id_when_no_caller_run_exists(monkeypatch):
    """Unlike `finish_plan_iteration`, a Target can always be created with
    no Plan context at all — this must not become a hard error, just a
    Target with no claim (the backend leaves plan_id NULL)."""
    monkeypatch.delenv("AW_RUN_ID", raising=False)
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t"}).content)
    assert body["claimed_by_run_id"] is None


def test_create_target_overwrites_a_forged_run_id_even_with_no_caller_run(monkeypatch):
    """Review finding (PR #20): a walrus-guarded `if _rid := ...: args[...] =
    _rid` skips the overwrite entirely when no caller run resolves, leaving
    a model-supplied `claimed_by_run_id` in `args` untouched — exactly the
    forgery this field exists to rule out, and exactly the combination
    `test_create_target_ignores_a_model_supplied_claimed_by_run_id` above
    does NOT cover (it always sets AW_RUN_ID first). The fix overwrites
    unconditionally, including to None."""
    monkeypatch.delenv("AW_RUN_ID", raising=False)
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t",
                             "claimed_by_run_id": "someone-elses-run"}).content)
    assert body["claimed_by_run_id"] is None


def test_create_target_sends_claimed_by_run_id_even_with_an_explicit_plan_slug(monkeypatch):
    """Harmless either way — the backend only consults claimed_by_run_id
    when no plan_slug was given — but asserted so a future refactor that
    tries to "optimize" this into an either/or doesn't quietly start
    withholding it on the explicit-claim path."""
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    body = json.loads(_call(monkeypatch, "create_target",
                            {"slug": "us1924311", "name": "t",
                             "plan_slug": "the-plan"}).content)
    assert body["plan_slug"] == "the-plan"
    assert body["claimed_by_run_id"] == "run-abc"


# ---------------------------------------------------------------------------
# update_target — plan_slug is a plain forward too (no run-id injection:
# attach/detach is always an explicit, slug-named act).
# ---------------------------------------------------------------------------

def test_update_target_forwards_plan_slug(monkeypatch):
    req = _call(monkeypatch, "update_target", {"slug": "us1924311", "plan_slug": "the-plan"})
    assert req.method == "PUT"
    assert req.url.path == "/api/targets/us1924311"
    body = json.loads(req.content)
    assert body == {"plan_slug": "the-plan"}
