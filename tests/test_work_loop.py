"""``work_loop`` — the deterministic orchestrator retiring the
``kanban_check_deployed`` / ``list_kanban_cards_need_attention`` split.

Same split as test_deploy_reconcile.py/test_kanban_dispatch.py: pure
decision logic against a fake board and a fake ``list_runs`` probe, no real
Notion/AP-MT calls. The HTTP-level glue (QA dispatch, the Manager-dispatch
hook, aliasing at the MCP tool-call layer) lives in mcp_server.py and is not
exercised here — this file is about work_loop.py's own orchestration.

Run: python -m pytest tests/test_work_loop.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import deploy_reconcile as dr  # noqa: E402
from agents_platform_runners_app import kanban_dispatch as kd  # noqa: E402
from agents_platform_runners_app import work_loop as wl  # noqa: E402

NOW = 1_800_000_000.0  # arbitrary fixed epoch — no wall-clock dependency


def _iso(seconds_ago: float, *, z: bool = False) -> str:
    ts = dt.datetime.fromtimestamp(NOW - seconds_ago, tz=dt.timezone.utc)
    text = ts.isoformat()
    return text.replace("+00:00", "Z") if z else text


# ── fake board ────────────────────────────────────────────────────────────

class FakeBoard:
    """Mirrors test_deploy_reconcile.FakeBoard for the deploy lane (so the
    aliasing test can run the identical scenario through both
    ``check_deployed`` directly and through work_loop's deploy loop), plus
    a ``need_attention``/``card`` surface for the attention loop."""

    def __init__(self, *, deploy_cards: dict | None = None,
                attention_result: dict | None = None,
                comments_by_page: dict | None = None):
        self._deploy_cards = deploy_cards or {}
        self._attention_result = attention_result or {
            "has_work": False, "cards": [], "counts": {"count": 0, "scanned": 0, "truncated": False}}
        self._comments_by_page = comments_by_page or {}
        self.need_attention_calls: list[dict] = []
        self.moves: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, object]] = []

    # -- deploy lane (identical shape to test_deploy_reconcile.FakeBoard) --
    def ready_cards(self, status="ready", limit=50):
        return [{"page_id": pid, "title": c.get("Name", pid), "status": c["Status"]}
                for pid, c in self._deploy_cards.items() if c["Status"] == status][:limit]

    def properties(self, page_id, names=None):
        card = self._deploy_cards.get(page_id) or {}
        return dict(card) if not names else {k: card.get(k) for k in names}

    def move(self, page_id, status, comment=""):
        self.moves.append((page_id, status))
        if comment:
            self.comments.append((page_id, comment))
        notion_name = {"done": "Done", "need_human": "Need Human"}.get(status, status)
        if page_id in self._deploy_cards:
            self._deploy_cards[page_id]["Status"] = notion_name
        return {"ok": True}

    def set_property(self, page_id, name, value):
        self.writes.append((page_id, name, value))
        if page_id in self._deploy_cards:
            self._deploy_cards[page_id][name] = value
        return {"ok": True}

    def add_comment(self, page_id, text):
        self.comments.append((page_id, text))
        return {"ok": True}

    # -- attention lane --
    def need_attention(self, *, limit=10, reping=True):
        self.need_attention_calls.append({"limit": limit, "reping": reping})
        return dict(self._attention_result)

    def card(self, page_id, *, with_content=True):
        return {"comments_md": self._comments_by_page.get(page_id, "")}


def _attention_card(page_id="p1", status="In Progress", last_swept_at=None, **over):
    base = {"page_id": page_id, "title": "Some card", "status": status,
            "last_swept_at": last_swept_at, "last_human_ping_at": None, "human_ping_count": 0}
    base.update(over)
    return base


def make_list_runs(responses: dict):
    """``responses``: ``{(notion_task_id, category): (rows, error)}``.
    ``category`` is ``None`` for the first (liveness) call."""
    calls: list[dict] = []

    async def _list_runs(*, notion_task_id=None, category=None, limit=20):
        calls.append({"notion_task_id": notion_task_id, "category": category, "limit": limit})
        return responses.get((notion_task_id, category), ([], None))

    _list_runs.calls = calls
    return _list_runs


def _ctx(board, list_runs=None, **over):
    kwargs = {"board": board, "list_runs": list_runs or make_list_runs({}), "now": NOW}
    kwargs.update(over)
    return wl.WorkLoopContext(**kwargs)


def _run(ctx, loops):
    return asyncio.run(wl.run_work_loop(ctx, loops))


# ── response shape / loops_skipped ──────────────────────────────────────

def test_response_has_the_full_envelope_even_with_nothing_to_do():
    result = _run(_ctx(FakeBoard()), ["deploy", "attention"])
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert "deploy" in result and "attention" in result
    assert result["work_orders"] == []
    assert result["actions_taken"] == []
    assert result["loops_skipped"] == []
    assert result["errors"] == []


def test_reserved_loops_are_skipped_not_errored():
    result = _run(_ctx(FakeBoard()), ["plans"])
    assert result["loops_skipped"] == ["plans: not implemented"]
    assert result["errors"] == []
    assert "plans" not in result


def test_unknown_loop_name_is_skipped_with_its_own_reason():
    result = _run(_ctx(FakeBoard()), ["deploy", "bogus"])
    assert "bogus: unknown loop" in result["loops_skipped"]
    assert "deploy" in result  # the other requested loop still ran


def test_retired_prs_loop_name_is_skipped_as_unknown_not_an_error():
    """The ``prs`` loop (card 4175bf3b-9510-8163) was retired in card
    4185bf3b-9510-81a7, superseded by aw-app-git's github-maintenance gate +
    Plan. A caller still requesting it by name must degrade gracefully, the
    same as any other unrecognized loop name — never a crash or a KeyError."""
    result = _run(_ctx(FakeBoard()), ["deploy", "prs"])
    assert "prs: unknown loop" in result["loops_skipped"]
    assert "prs" not in result
    assert "deploy" in result
    assert result["errors"] == []


def test_a_dead_loop_does_not_take_down_the_others():
    class ExplodingBoard(FakeBoard):
        def ready_cards(self, status="ready", limit=50):
            raise RuntimeError("boom")

    result = _run(_ctx(ExplodingBoard()), ["deploy", "attention"])
    assert result["errors"] == [{"loop": "deploy", "error": "boom"}]
    assert "attention" in result  # attention still ran despite deploy's crash
    assert result["ok"] is True, "a PARTIAL failure (one of two loops) is still a usable report"


def test_ok_is_false_only_when_every_attempted_loop_failed():
    """Code-review finding (PR #36, round 1 — left unfixed in round 2):
    run_work_loop hardcoded ok:True regardless of `errors`, which made
    mcp_server.py's `except BoardUnavailable: return _err(503, ...)` guard
    around this call dead code — a total board outage used to come back as
    ok:true with the errors buried in a list nobody was told to check."""
    class ExplodingBoard(FakeBoard):
        def ready_cards(self, status="ready", limit=50):
            raise kd.BoardUnavailable("board is down")

        def need_attention(self, *, limit=10, reping=True):
            raise kd.BoardUnavailable("board is down")

    result = _run(_ctx(ExplodingBoard()), ["deploy", "attention"])
    assert len(result["errors"]) == 2
    assert result["ok"] is False


def test_loops_run_concurrently_not_sequentially(monkeypatch):
    """The whole point of the ``asyncio.gather`` rewrite (bug:work-loop-
    multi-loop-timeout): wall-clock cost for N independent loops must be the
    SLOWEST one, not the SUM of all of them — the sequential ``for`` loop
    this replaces paid the full sum as its own critical path, which is what
    pushed ``work_loop(loops=["deploy","attention","ready"])`` past the
    caller's tool-call timeout even though each loop alone was fast."""
    SLEEP_S = 0.05

    async def _slow_a(ctx):
        await asyncio.sleep(SLEEP_S)
        return {"name": "a"}, [], []

    async def _slow_b(ctx):
        await asyncio.sleep(SLEEP_S)
        return {"name": "b"}, [], []

    monkeypatch.setitem(wl.LOOP_FUNCS, "deploy", _slow_a)
    monkeypatch.setitem(wl.LOOP_FUNCS, "attention", _slow_b)

    start = time.monotonic()
    result = _run(_ctx(FakeBoard()), ["deploy", "attention"])
    elapsed = time.monotonic() - start

    assert result["deploy"] == {"name": "a"}
    assert result["attention"] == {"name": "b"}
    # Sequential would cost >= 2 * SLEEP_S; concurrent costs ~= 1 * SLEEP_S.
    assert elapsed < SLEEP_S * 1.5, f"loops ran sequentially (elapsed={elapsed})"


def test_ok_stays_true_when_only_reserved_loops_were_requested():
    """Nothing was actually attempted — reserved/unknown loops don't count
    toward the total-failure check, so this is a no-op report, not a
    failure."""
    result = _run(_ctx(FakeBoard()), ["plans"])
    assert result["errors"] == []
    assert result["ok"] is True


# ── dry_run totality ─────────────────────────────────────────────────────

def test_dry_run_forces_reping_off_even_if_the_caller_asked_for_it():
    card = _attention_card(last_swept_at=None)
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False},
                                        "re_pinged": [], "reping": {"due": 0}})
    _run(_ctx(board, dry_run=True, reping=True), ["attention"])
    assert board.need_attention_calls == [{"limit": wl.DEFAULT_ATTENTION_LIMIT, "reping": False}]


def test_dry_run_makes_no_board_writes_in_the_deploy_loop():
    board = FakeBoard(deploy_cards={
        "p1": {"Name": "X", "Status": "Ready to Deploy", "TargetSlug": "t1", "QAStatus": "",
               "DeployEnv": "prod", "DeployComponent": "", "DeployWorkspaces": [],
               "DeployToVersion": "", "DeployFromVersion": "", "DeployCheckResult": ""}})
    result = _run(_ctx(board, dry_run=True), ["deploy"])
    assert result["deploy"]["dry_run"] is True
    assert board.moves == [] and board.comments == [] and board.writes == []


# ── attention loop: back-off ─────────────────────────────────────────────

def test_backoff_skips_a_card_before_the_run_check_ever_fires():
    card = _attention_card(last_swept_at=_iso(3600))  # swept 1h ago
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}},
                      comments_by_page={"p1": f"- **{_iso(7200)}** — an older comment"})
    list_runs = make_list_runs({})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["backoff"] == "backed_off"
    assert candidate["agent_activity"]["state"] == "unknown"
    assert candidate["qualifies_for_dispatch"] is False
    assert list_runs.calls == []  # never reached the run-check
    assert result["work_orders"] == []


def test_a_comment_after_last_swept_at_re_arms_eligibility():
    card = _attention_card(last_swept_at=_iso(7200))  # swept 2h ago
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}},
                      comments_by_page={"p1": f"- **{_iso(60)}** — fresh comment, 1 min ago"})
    list_runs = make_list_runs({("p1", None): ([], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["backoff"] == "eligible"
    assert list_runs.calls  # the run-check DID fire


def test_unreadable_comments_fail_open_to_eligible():
    card = _attention_card(last_swept_at=_iso(3600))
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})

    def _raise_card(page_id, *, with_content=True):
        raise kd.BoardUnavailable("simulated Notion 5xx")
    board.card = _raise_card
    list_runs = make_list_runs({("p1", None): ([], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    assert result["attention"]["candidates"][0]["backoff"] == "eligible"


# ── attention loop: agent_activity / run-check ───────────────────────────

def test_no_run_at_all_qualifies_as_state_none():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({("p1", None): ([], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["agent_activity"]["state"] == "none"
    assert candidate["qualifies_for_dispatch"] is True
    assert result["work_orders"] == [{
        "page_id": "p1", "reason": "In Progress card, no live agent activity",
        "evidence": "no dispatched run found for this card",
        "suggested_action": "needs routing — no prior run to resume"}]


def test_a_running_row_is_live_and_never_qualifies():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({
        ("p1", None): ([{"id": "r1", "status": "running", "source_slug": "coder-sonnet"}], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["agent_activity"] == {"state": "live", "live_run_id": "r1",
                                           "source_slug": "coder-sonnet"}
    assert candidate["qualifies_for_dispatch"] is False
    assert result["work_orders"] == []


def test_a_recently_terminal_row_is_recent_and_never_qualifies():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({
        ("p1", None): ([{"id": "r1", "status": "success", "ended_at": _iso(10 * 60)}], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["agent_activity"]["state"] == "recent"
    assert candidate["qualifies_for_dispatch"] is False


def test_a_stale_terminal_row_resumes_via_the_pinned_non_qa_lookup():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({
        ("p1", None): ([{"id": "r-newest", "status": "success", "ended_at": _iso(40 * 60)}], None),
        ("p1", "work"): ([
            {"id": "r-qa", "status": "success", "ended_at": _iso(41 * 60), "source_slug": "qa-sonnet"},
            {"id": "r-real", "status": "success", "ended_at": _iso(45 * 60), "source_slug": "coder-sonnet"},
        ], None),
    })
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    activity = candidate["agent_activity"]
    assert activity["state"] == "stale"
    assert activity["resume_run_id"] == "r-real", (
        "walk order matters: the FIRST non-qa-* row in AP-MT's own newest-first "
        "ordering wins — r-qa here is the newest overall and is qa-*, so it's "
        "excluded; r-real is next and is the one picked")
    assert "resume_window_exceeded" not in activity
    assert candidate["qualifies_for_dispatch"] is True
    assert result["work_orders"][0]["suggested_action"] == (
        f"resume via invoke_kanban_agent run_id={activity['resume_run_id']}")


def test_all_qa_rows_in_the_window_sets_resume_window_exceeded():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({
        ("p1", None): ([{"id": "r-newest", "status": "success", "ended_at": _iso(40 * 60)}], None),
        ("p1", "work"): ([
            {"id": "r-qa1", "status": "success", "ended_at": _iso(41 * 60), "source_slug": "qa-sonnet"},
            {"id": "r-qa2", "status": "success", "ended_at": _iso(50 * 60), "source_slug": "qa-haiku"},
        ], None),
    })
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    activity = result["attention"]["candidates"][0]["agent_activity"]
    assert activity["state"] == "stale"
    assert activity["resume_run_id"] == "r-qa1"  # fallback: newest row anyway
    assert activity["resume_window_exceeded"] is True


def test_no_work_run_at_all_after_a_stale_monitor_row_is_state_none():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({
        ("p1", None): ([{"id": "r-monitor", "status": "success", "ended_at": _iso(40 * 60)}], None),
        ("p1", "work"): ([], None),
    })
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["agent_activity"]["state"] == "none"
    assert candidate["qualifies_for_dispatch"] is True


def test_a_probe_error_fails_closed_to_unknown_and_never_qualifies():
    card = _attention_card()
    board = FakeBoard(attention_result={"has_work": True, "cards": [card],
                                        "counts": {"count": 1, "scanned": 1, "truncated": False}})
    list_runs = make_list_runs({("p1", None): (None, "AP-MT unreachable")})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidate = result["attention"]["candidates"][0]
    assert candidate["agent_activity"] == {"state": "unknown", "error": "AP-MT unreachable"}
    assert candidate["qualifies_for_dispatch"] is False
    assert result["work_orders"] == []


def test_run_check_probe_cap_stops_probing_but_still_reports_every_candidate(monkeypatch):
    monkeypatch.setattr(wl, "MAX_RUN_CHECK_PROBES", 1)
    cards = [_attention_card(page_id="p1"), _attention_card(page_id="p2")]
    board = FakeBoard(attention_result={"has_work": True, "cards": cards,
                                        "counts": {"count": 2, "scanned": 2, "truncated": False}})
    list_runs = make_list_runs({("p1", None): ([], None), ("p2", None): ([], None)})
    result = _run(_ctx(board, list_runs=list_runs), ["attention"])
    candidates = {c["page_id"]: c for c in result["attention"]["candidates"]}
    assert candidates["p1"]["agent_activity"]["state"] == "none"
    assert candidates["p2"]["agent_activity"] == {"state": "unknown",
                                                   "reason": "run-check probe cap reached"}
    assert len(list_runs.calls) == 1  # p2 never reached the probe


def test_re_pinged_cards_become_actions_taken():
    board = FakeBoard(attention_result={
        "has_work": True, "cards": [], "counts": {"count": 0, "scanned": 0, "truncated": False},
        "re_pinged": [{"page_id": "p9", "human_ping_count": 2}], "reping": {"due": 1}})
    result = _run(_ctx(board), ["attention"])
    assert result["actions_taken"] == [{"loop": "attention", "page_id": "p9",
                                        "action": "re_pinged", "detail": "human_ping_count=2"}]


def test_page_id_single_card_mode_bypasses_need_attention_scan():
    board = FakeBoard(deploy_cards={"p1": {"Name": "X", "Status": "In Progress",
                                           "LastSweptAt": None, "LastHumanPingAt": None,
                                           "HumanPingCount": None}})
    list_runs = make_list_runs({("p1", None): ([], None)})
    result = _run(_ctx(board, list_runs=list_runs, page_id="p1"), ["attention"])
    assert board.need_attention_calls == []
    assert result["attention"]["candidates"][0]["page_id"] == "p1"


def test_page_id_single_card_mode_skips_a_card_outside_the_attention_statuses():
    """Code-review finding, PR #36: single-card mode used to run the full
    backoff + agent_activity pipeline regardless of status, so a page_id
    naming a Done/Backlog card could emit a work order recommending
    resumption for a card that isn't stuck at all."""
    board = FakeBoard(deploy_cards={"p1": {"Name": "X", "Status": "Done",
                                           "LastSweptAt": None, "LastHumanPingAt": None,
                                           "HumanPingCount": None}})
    list_runs = make_list_runs({})
    result = _run(_ctx(board, list_runs=list_runs, page_id="p1"), ["attention"])
    assert result["attention"]["candidates"] == []
    assert result["work_orders"] == []
    assert list_runs.calls == []  # never reached the run-check


# ── ready loop ────────────────────────────────────────────────────────────

def test_ready_loop_without_a_platform_client_reports_an_error_not_a_crash():
    result = _run(_ctx(FakeBoard()), ["ready"])
    assert "error" in result["ready"]
    assert result["errors"] == []  # an explainable per-loop section, not a crashed loop


# ── deploy loop / aliasing ────────────────────────────────────────────────

def _deploy_card(**over):
    base = {"Name": "Deploy X", "Status": "Ready to Deploy", "TargetSlug": "t1",
            "QAStatus": "", "DeployEnv": "prod", "DeployComponent": "app:notion",
            "DeployWorkspaces": [], "DeployToVersion": "v0.35.1",
            "DeployFromVersion": "", "DeployCheckResult": ""}
    base.update(over)
    return base


def test_deploy_loop_result_is_byte_identical_to_calling_check_deployed_directly(monkeypatch):
    """The 'aliasing' guarantee (card instructions item #4): work_loop's
    deploy section is not a reshaped or re-derived copy of check_deployed's
    own contract, it IS that contract, verbatim — so an existing
    kanban_check_deployed caller sees no difference once it's wired through
    work_loop's shared internals."""
    _patch_observe = lambda klass, result: monkeypatch.setitem(dr.ADAPTERS, klass, lambda n, w: result)
    _patch_observe("app", {"observed": "v0.35.1", "error": None})

    cards = {"p1": _deploy_card()}
    board_a = FakeBoard(deploy_cards={k: dict(v) for k, v in cards.items()})
    board_b = FakeBoard(deploy_cards={k: dict(v) for k, v in cards.items()})

    direct = dr.check_deployed(board_a, max_cards=10)
    via_work_loop = _run(_ctx(board_b), ["deploy"])["deploy"]
    assert via_work_loop == direct


def test_deploy_loop_contributes_no_work_orders_ever():
    """Every deploy verdict is already fully deterministic end-to-end — the
    Manager is never handed a deploy-lane decision to make."""
    board = FakeBoard(deploy_cards={"p1": _deploy_card(DeployComponent="")})
    result = _run(_ctx(board), ["deploy"])
    assert result["work_orders"] == []
    assert result["deploy"]["still_waiting"][0]["verdict"] == "invalid"
