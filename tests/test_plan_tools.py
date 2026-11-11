"""The Plan MCP surface — Plan umbrella Slice 3 and Slice 5
(AP-MT Kanban 4115bf3b-9510-8134-b9e6-e15db67189cd,
4155bf3b-9510-81bd-b1c9-dda6df622e73).

Four Slice-3 tools, three of them plain reads and one terminal, plus
Slice 5's management surface (list_plans/create_plan/activate_plan/
update_plan/record_plan_criterion). What this file tests is the part that
is this layer's own responsibility and nobody else's: that each tool
reaches the right AP-MT endpoint with the right body, and that
`finish_plan_iteration` fills in the caller's run id ITSELF.

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
from typing import Any

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


def _patch_client_sequence(monkeypatch, recorder: list, responses: list[tuple[int, Any]]):
    """Like `_patch_client`, but for a tool that makes MORE than one HTTP
    call (`record_plan_criterion`: a GET to read the current document, then
    a PUT to write it back) — each entry in `responses` answers one call in
    order."""
    calls = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        status, payload = calls.pop(0)
        return httpx.Response(status, json=payload)

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", fake_async_client)


def _call_seq(monkeypatch, name: str, args: dict,
             responses: list[tuple[int, Any]]) -> tuple[list[httpx.Request], list]:
    recorder: list = []
    _patch_client_sequence(monkeypatch, recorder, responses)
    out = asyncio.run(mcp_server._call_tool(name, args))
    return recorder, out


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


def test_list_plans_reaches_the_collection_endpoint(monkeypatch):
    req = _call(monkeypatch, "list_plans", {}, payload=[])
    assert req.method == "GET"
    assert req.url.path == "/api/plans"


def test_list_plans_forwards_its_filters(monkeypatch):
    req = _call(monkeypatch, "list_plans",
               {"status": "active", "q": "growth", "limit": 10,
                "include_deleted": True}, payload=[])
    assert req.url.params["status"] == "active"
    assert req.url.params["q"] == "growth"
    assert req.url.params["limit"] == "10"
    assert req.url.params["include_deleted"] == "true"


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


def test_only_the_read_terminal_and_management_plan_tools_exist(monkeypatch):
    """No `conclude_plan`, no `reopen_plan`, no pause/resume/delete, no
    force, no waive — at any point, now or later.

    "Force/waive/reopen/pause/resume/delete remain human-authorized" is a
    product decision repeated across several cards (most recently
    4155bf3b-9510-81bd-b1c9-dda6df622e73, which ships the rest of this
    management surface), and the way it is kept is that no tool exposes
    them. This test is what makes adding one a deliberate act rather than
    an oversight.
    """
    names = {t.name for t in asyncio.run(mcp_server._list_tools())}
    assert {"list_plans", "get_plan", "plan_summary", "list_plan_iterations",
            "create_plan", "activate_plan", "update_plan",
            "record_plan_criterion", "finish_plan_iteration"} <= names
    assert not {"conclude_plan", "reopen_plan", "pause_plan", "resume_plan",
                "delete_plan", "force_conclude_plan",
                "waive_plan_criterion"} & names


# ---------------------------------------------------------------------------
# Plan MANAGEMENT — Slice 5 (AP-MT Kanban 4155bf3b-9510-81bd-b1c9-dda6df622e73)
# ---------------------------------------------------------------------------

def test_create_plan_forwards_only_the_exposed_fields(monkeypatch):
    req = _call(monkeypatch, "create_plan",
               {"slug": "biz-growth", "name": "Growth", "kind": "operation",
                "purpose": "grow MRR", "needs_strategy": "see doc",
                "delivery_criteria": [{"text": "x"}], "cadence_seconds": 3600,
                "budget_tokens": 1000, "budget_usd": 5.0,
                "auto_conclude": False, "follow_up_agent_slug": "business-steward",
                # Not part of PlanIn at all — would 422 if forwarded as-is.
                "bogus_field": "nope"})
    assert req.method == "POST"
    assert req.url.path == "/api/plans"
    body = json.loads(req.content)
    assert body == {"slug": "biz-growth", "name": "Growth", "kind": "operation",
                    "purpose": "grow MRR", "needs_strategy": "see doc",
                    "delivery_criteria": [{"text": "x"}], "cadence_seconds": 3600,
                    "budget_tokens": 1000, "budget_usd": 5.0,
                    "auto_conclude": False, "follow_up_agent_slug": "business-steward"}


def test_create_plan_declares_slug_and_name_as_required():
    schema = _tool("create_plan").inputSchema
    assert set(schema["required"]) == {"slug", "name"}


def test_create_plan_description_tells_the_agent_to_set_auto_conclude_false():
    """The column default is `true` — this is the one place an agent learns
    that silence here reverses the propose-only behavior a business plan
    needs, rather than merely leaving it unset."""
    desc = _tool("create_plan").description
    assert "auto_conclude=false" in desc
    assert "default is" in desc.lower() and "true" in desc.lower()


def test_activate_plan_reaches_the_activate_endpoint(monkeypatch):
    req = _call(monkeypatch, "activate_plan", {"slug": "biz-growth"})
    assert req.method == "POST"
    assert req.url.path == "/api/plans/biz-growth/activate"


def test_activate_plan_surfaces_the_gate_violations_verbatim(monkeypatch):
    """A refused activation names every missing/invalid field at once — the
    whole point is lost if this tool summarized or swallowed it."""
    recorder: list = []
    _patch_client(monkeypatch, recorder, status=400,
                  payload={"detail": "activation gate failed: purpose is "
                                     "required; needs at least one required "
                                     "delivery criterion"})
    out = asyncio.run(mcp_server._call_tool("activate_plan", {"slug": "biz-growth"}))
    body = json.loads(out[0].text)
    assert body["status"] == 400
    assert "purpose is required" in body["message"]
    assert "required delivery criterion" in body["message"]


# -- update_plan: the field restriction is the whole point of this tool --

def test_update_plan_forwards_only_the_allowed_fields(monkeypatch):
    req = _call(monkeypatch, "update_plan",
               {"slug": "biz-growth", "purpose": "new purpose",
                "needs_strategy": "new strategy", "cadence_seconds": 7200,
                "budget_tokens": 2000, "budget_usd": 10.0, "tags": ["q4"]})
    assert req.method == "PUT"
    assert req.url.path == "/api/plans/biz-growth"
    assert json.loads(req.content) == {
        "purpose": "new purpose", "needs_strategy": "new strategy",
        "cadence_seconds": 7200, "budget_tokens": 2000, "budget_usd": 10.0,
        "tags": ["q4"]}


def test_update_plan_drops_status_kind_auto_conclude_and_follow_up_agent(monkeypatch):
    """Mutation test (card's own instruction): these four are present in
    the call but MUST NOT reach AP-MT, because AP-MT's own `PlanUpdate`
    accepts `auto_conclude`/`follow_up_agent_slug` and would apply them if
    this layer forwarded them — letting an agent hand itself conclusion
    authority or redirect its own steward assignment."""
    req = _call(monkeypatch, "update_plan",
               {"slug": "biz-growth", "purpose": "legit edit",
                "status": "concluded", "kind": "operation",
                "auto_conclude": True, "follow_up_agent_slug": "evil-agent"})
    body = json.loads(req.content)
    assert body == {"purpose": "legit edit"}
    for excluded in ("status", "kind", "auto_conclude", "follow_up_agent_slug"):
        assert excluded not in body


def test_update_plan_schema_does_not_even_declare_the_excluded_fields():
    schema = _tool("update_plan").inputSchema
    for excluded in ("status", "kind", "auto_conclude", "follow_up_agent_slug"):
        assert excluded not in schema["properties"]


# -- record_plan_criterion: read-modify-write over one criterion --

def test_record_plan_criterion_reads_then_writes_the_whole_document(monkeypatch):
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    plan = {"slug": "biz-growth",
            "delivery_criteria": [
                {"id": "c-1", "text": "ship MVP", "required": True,
                 "status": "pending", "evidence": []},
                {"id": "c-2", "text": "other", "required": False,
                 "status": "pending", "evidence": []},
            ]}
    recorder, out = _call_seq(monkeypatch, "record_plan_criterion",
                              {"slug": "biz-growth", "criterion_id": "c-1",
                               "status": "satisfied", "note": "shipped",
                               "url": "https://github.com/x/y/pull/1"},
                              [(200, plan), (200, plan)])
    assert len(recorder) == 2, "expected a GET then a PUT"
    get_req, put_req = recorder
    assert get_req.method == "GET"
    assert get_req.url.path == "/api/plans/biz-growth"
    assert put_req.method == "PUT"
    assert put_req.url.path == "/api/plans/biz-growth/criteria"
    sent = json.loads(put_req.content)["delivery_criteria"]
    c1 = next(c for c in sent if c["id"] == "c-1")
    c2 = next(c for c in sent if c["id"] == "c-2")
    assert c1["status"] == "satisfied"
    assert c1["evidence"] == [{"run_id": "run-abc", "note": "shipped",
                               "url": "https://github.com/x/y/pull/1"}]
    # The untouched criterion travels through unchanged — this is a
    # whole-document PUT, so dropping it would silently delete it.
    assert c2 == {"id": "c-2", "text": "other", "required": False,
                  "status": "pending", "evidence": []}


def test_record_plan_criterion_evidence_always_carries_the_callers_run_id(monkeypatch):
    """Never model-supplied — same rule `finish_plan_iteration`'s `run_id`
    and `create_target`'s `claimed_by_run_id` follow. Evidence that didn't
    carry who actually produced it would be unverifiable."""
    monkeypatch.setenv("AW_RUN_ID", "run-real")
    plan = {"delivery_criteria": [{"id": "c-1", "text": "x", "required": True,
                                   "status": "pending", "evidence": []}]}
    recorder, _ = _call_seq(monkeypatch, "record_plan_criterion",
                            {"slug": "p", "criterion_id": "c-1",
                             "run_id": "someone-elses-run"},
                            [(200, plan), (200, plan)])
    sent = json.loads(recorder[1].content)["delivery_criteria"][0]
    assert sent["evidence"] == [{"run_id": "run-real"}]


def test_record_plan_criterion_refuses_with_no_caller_run_id(monkeypatch):
    """No caller run means no provenance for the evidence this tool exists
    to append — must be a visible error, not evidence recorded with a null
    run_id no one can trace back."""
    monkeypatch.delenv("AW_RUN_ID", raising=False)
    recorder: list = []
    _patch_client(monkeypatch, recorder)
    out = asyncio.run(mcp_server._call_tool(
        "record_plan_criterion", {"slug": "p", "criterion_id": "c-1"}))
    assert recorder == [], "it called the backend with no provenance for the evidence"
    body = json.loads(out[0].text)
    assert body["status"] == 400
    assert "provenance" in body["message"]


def test_record_plan_criterion_rejects_waived_before_calling_the_backend(monkeypatch):
    """'waived' stays human-only — refused here, before the GET even
    happens, rather than relying on AP-MT's own invariant, which only
    blocks waiving a REQUIRED criterion and would silently accept this for
    a non-required one."""
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    recorder: list = []
    _patch_client(monkeypatch, recorder)
    out = asyncio.run(mcp_server._call_tool(
        "record_plan_criterion",
        {"slug": "p", "criterion_id": "c-1", "status": "waived"}))
    assert recorder == [], "it reached the backend with a disallowed status"
    body = json.loads(out[0].text)
    assert body["status"] == 400
    assert "waived" in body["message"]


def test_record_plan_criterion_404s_on_an_unknown_criterion_id(monkeypatch):
    monkeypatch.setenv("AW_RUN_ID", "run-abc")
    plan = {"delivery_criteria": [{"id": "c-1", "text": "x", "required": True,
                                   "status": "pending", "evidence": []}]}
    recorder, out = _call_seq(monkeypatch, "record_plan_criterion",
                              {"slug": "p", "criterion_id": "does-not-exist"},
                              [(200, plan)])
    assert len(recorder) == 1, "must not PUT after the criterion isn't found"
    body = json.loads(out[0].text)
    assert body["status"] == 404


def test_record_plan_criterion_schema_excludes_waived():
    enum = _tool("record_plan_criterion").inputSchema["properties"]["status"]["enum"]
    assert "waived" not in enum
    assert set(enum) == {"pending", "satisfied"}


# ---------------------------------------------------------------------------
# Plan MEMORY — Slice 8 (AP-MT Kanban 4135bf3b-9510-819c-bef0-d50d2440cf90)
# ---------------------------------------------------------------------------

def test_get_plan_memory_reaches_the_memory_endpoint(monkeypatch):
    req = _call(monkeypatch, "get_plan_memory", {"slug": "my-plan"},
                payload={"memory": {}, "version": 0})
    assert req.method == "GET"
    assert req.url.path == "/api/plans/my-plan/memory"


def test_update_plan_memory_writes_the_callers_OWN_ext_subtree(monkeypatch):
    """The writer key goes in the PATH, not the body.

    That is the whole scoping contract: the endpoint replaces
    `ext[<writer_key>]` and nothing else, and it is what lets this tool take
    no version echo. A handler that sent the key in the body instead would
    hit `PUT /memory` — the human FULL-REPLACE path — which would blank
    every other writer's subtree and 409 on the missing version.
    """
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "kanban-sweeper",
                 "value": {"cursor": "abc"}},
                payload={"memory": {}, "version": 1})
    assert req.method == "PUT"
    assert req.url.path == "/api/plans/my-plan/memory/ext/kanban-sweeper"
    assert json.loads(req.content) == {"value": {"cursor": "abc"}}


def test_update_plan_memory_OMITS_the_shared_fields_it_was_not_given(monkeypatch):
    """`summary`/`state` are shared, and absent means "leave them alone".

    Sending them as explicit nulls would be the same thing server-side
    today, but it makes this layer depend on that reading; omitting them
    keeps the contract in one place. Getting this wrong in the other
    direction — sending `""` — would silently blank the summary every
    steward prompt renders.
    """
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "a", "value": 1},
                payload={"memory": {}, "version": 1})
    body = json.loads(req.content)
    assert "summary" not in body
    assert "state" not in body


def test_update_plan_memory_forwards_the_shared_fields_when_given(monkeypatch):
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "a", "value": 1,
                 "summary": "two cards left", "state": {"pass": 4}},
                payload={"memory": {}, "version": 1})
    assert json.loads(req.content) == {"value": 1, "summary": "two cards left",
                                       "state": {"pass": 4}}


def test_a_null_value_is_still_sent(monkeypatch):
    """`ext["x"] = null` is a writer saying "I have no state", which is
    different from the key being absent — so `value` is always in the body,
    unlike the two optional shared fields."""
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "a", "value": None},
                payload={"memory": {}, "version": 1})
    assert json.loads(req.content) == {"value": None}


def test_update_plan_memory_declares_what_it_requires():
    """A required field missing from the schema is a field the model will
    omit, producing a 422 the agent reads as a tool bug."""
    schema = _tool("update_plan_memory").inputSchema
    assert set(schema["required"]) == {"slug", "writer_key", "value"}
    assert set(schema["properties"]) == {"slug", "writer_key", "value",
                                         "summary", "state"}


def test_update_plan_memory_auto_decodes_a_double_encoded_object(monkeypatch):
    """Two live kanban-sweeper runs passed a `json.dumps(...)`'d STRING as
    `value` instead of the native object they meant (card
    4145bf3b-9510-81c1-b5c5-f7e5be2ae13d, 2026-11-11) — `ext["kanban-
    sweeper"]` landed in `Plan.memory` as a string, so a reader had to
    parse it before reaching `.iterations[0]`. `inputSchema` here is
    advisory only (this server has no jsonschema validation at all, see
    the dispatch branch), so the fix has to live in the dispatch, not the
    schema: a string that itself decodes to a dict/list is auto-corrected
    to the decoded object before it is forwarded."""
    double_encoded = json.dumps({"schema_version": 1, "iterations": [{"seq": 1}]})
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "a", "value": double_encoded},
                payload={"memory": {}, "version": 1})
    assert json.loads(req.content) == {
        "value": {"schema_version": 1, "iterations": [{"seq": 1}]}}


def test_update_plan_memory_leaves_a_genuine_string_value_alone(monkeypatch):
    """Not every string is a mistake — a steward whose whole ext value IS a
    plain string (not JSON-shaped) must still be able to write it."""
    req = _call(monkeypatch, "update_plan_memory",
                {"slug": "my-plan", "writer_key": "a", "value": "just a note"},
                payload={"memory": {}, "version": 1})
    assert json.loads(req.content) == {"value": "just a note"}


def test_the_memory_descriptions_carry_the_three_rules_an_agent_needs():
    """The prompt already carries summary/state/version/ext_keys every
    cycle, so the two things an agent can only learn here are: use a STABLE
    writer key, and omitting `summary` preserves it. The 64 KiB ceiling is
    the third — a 413 with no explanation reads as a transient failure to
    retry, which is the worst possible reaction to it."""
    desc = _tool("update_plan_memory").description
    assert "stable key" in desc
    assert "omit" in desc.lower()
    assert "64 KiB" in desc
    assert "413" in desc
    read_desc = _tool("get_plan_memory").description
    assert "ext" in read_desc


def test_the_memory_tools_exist_and_no_full_replace_tool_does(monkeypatch):
    """There must be no agent-facing FULL-REPLACE tool.

    The whole-document `PUT /memory` is the human editor's path: it takes a
    version echo, it 409s on a stale one, and it can blank every other
    writer's subtree. An agent reaching it would turn a scoped write that
    cannot lose data into one that can — the same "no tool exposes it"
    discipline force/waive/reopen already get above.
    """
    names = {t.name for t in asyncio.run(mcp_server._list_tools())}
    assert {"get_plan_memory", "update_plan_memory"} <= names
    assert not {"replace_plan_memory", "put_plan_memory",
                "set_plan_memory"} & names
