"""kanban_check_deployed — deterministic Ready-to-Deploy reconciliation.

Same split as test_kanban_dispatch.py: the pure decision logic (component
parsing, version comparison, verdict aggregation, transition-only
move/comment) is exercised against a fake board with no real Notion/aw-backend
calls, same as kanban_dispatch's own tests.

Run: python -m pytest tests/test_deploy_reconcile.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import deploy_reconcile as dr  # noqa: E402

# ── component parsing ────────────────────────────────────────────────────

def test_parse_component_scalars():
    assert dr.parse_component("core") == ("core", "")
    assert dr.parse_component("remote-host") == ("remote-host", "")


def test_parse_component_namespaced():
    assert dr.parse_component("app:notion") == ("app", "notion")
    assert dr.parse_component("service:aw-backend") == ("service", "aw-backend")


def test_parse_component_unknown_class():
    klass, name = dr.parse_component("widget:foo")
    assert klass is None


# ── version comparison ───────────────────────────────────────────────────

def test_semver_parse_and_compare():
    assert dr.parse_semver("v0.35.0") == (0, 35, 0)
    assert dr.compare("app", "v0.35.1", "v0.35.0") == "deployed"
    assert dr.compare("app", "v0.34.9", "v0.35.0") == "pending"


def test_compare_treats_equal_as_newer_not_outdated():
    """Mirrors aw-backend's own outdated=False-on-equal rule."""
    assert dr.compare("core", "v0.1.80", "v0.1.80") == "deployed"


def test_compare_non_semver_is_unknown_not_outdated():
    """Mirrors aw-backend's test_non_semver_values_are_unknown_not_outdated —
    a short SHA fallback must never be treated as comparable to a semver tag."""
    assert dr.compare("app", "abc1234", "v0.1.80") == "unknown"
    assert dr.compare("app", None, "v0.1.80") == "unknown"


def test_compare_service_sha_exact_match_only():
    sha = "a" * 40
    assert dr.compare("service", sha, sha) == "deployed"
    assert dr.compare("service", "b" * 40, sha) == "unknown", (
        "no ancestry check available in this runtime — a non-equal SHA is "
        "unknown, not assumed pending")


# ── fake board ────────────────────────────────────────────────────────────

class FakeBoard:
    def __init__(self, cards: dict[str, dict], fail_comment_for: set[str] | None = None,
                 fail_move_with_timeout_for: set[str] | None = None):
        """``cards``: {page_id: {**properties, "status": "..."}}.

        ``fail_comment_for``: page_ids whose add_comment() raises
        BoardUnavailable once — for the guard regression tests.

        ``fail_move_with_timeout_for``: page_ids whose move() raises a bare
        TimeoutError once — the real BoardClient's own un-translated shape
        for a response-level socket timeout (see test_kanban_dispatch.py's
        matching _request regression test), distinct from BoardUnavailable.
        """
        self._cards = cards
        self.moves: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, object]] = []
        self._fail_comment_for = set(fail_comment_for or ())
        self._fail_move_with_timeout_for = set(fail_move_with_timeout_for or ())

    def ready_cards(self, status="ready", limit=50):
        return [{"page_id": pid, "title": c.get("Name", pid), "status": c["Status"]}
                for pid, c in self._cards.items() if c["Status"] == status][:limit]

    def properties(self, page_id, names=None):
        card = self._cards[page_id]
        return dict(card) if not names else {k: card.get(k) for k in names}

    def move(self, page_id, status):
        if page_id in self._fail_move_with_timeout_for:
            self._fail_move_with_timeout_for.discard(page_id)
            raise TimeoutError("simulated response-level socket timeout")
        self.moves.append((page_id, status))
        notion_name = {"done": "Done", "need_human": "Need Human"}.get(status, status)
        self._cards[page_id]["Status"] = notion_name
        return {"ok": True}

    def set_property(self, page_id, name, value):
        self.writes.append((page_id, name, value))
        self._cards[page_id][name] = value
        return {"ok": True}

    def add_comment(self, page_id, text):
        if page_id in self._fail_comment_for:
            self._fail_comment_for.discard(page_id)
            raise dr.kanban_dispatch.BoardUnavailable("simulated Notion 5xx")
        self.comments.append((page_id, text))
        return {"ok": True}


def _card(**over):
    base = {"Name": "Deploy X", "Status": "Ready to Deploy", "TargetSlug": "t1",
            "QAStatus": "", "DeployEnv": "prod", "DeployComponent": "app:notion",
            "DeployWorkspaces": [], "DeployToVersion": "v0.35.1",
            "DeployFromVersion": "", "DeployCheckResult": ""}
    base.update(over)
    return base


def _patch_observe(monkeypatch, klass: str, result: dict):
    monkeypatch.setitem(dr.ADAPTERS, klass, lambda name, ws: result)


# ── per-card verdicts + transitions ─────────────────────────────────────

def test_invalid_component_is_skipped_and_commented_once(monkeypatch):
    board = FakeBoard({"p1": _card(DeployComponent="")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["verdict"] == "invalid"
    assert len(board.comments) == 1
    # Re-run: already reported, no second comment.
    result2 = dr.check_deployed(board, max_cards=10)
    assert result2["still_waiting"][0]["verdict"] == "invalid"
    assert len(board.comments) == 1
    assert not board.moves


def test_a_failed_comment_on_one_card_does_not_abort_the_whole_batch(monkeypatch):
    """Code-review finding on PR #25: check_deployed()'s verdict branches
    called board.add_comment()/board.move() with no guard against
    BoardUnavailable, so one flaky card would propagate out of the whole
    `for summary in candidates` loop and lose every other card's results."""
    board = FakeBoard({
        "p1": _card(DeployComponent="", Name="Bad card 1"),
        "p2": _card(DeployComponent="", Name="Bad card 2"),
    }, fail_comment_for={"p1"})
    result = dr.check_deployed(board, max_cards=10)
    assert result["ok"] is True
    assert result["considered"] == 2
    rows = {r["page_id"]: r for r in result["still_waiting"]}
    assert rows["p1"]["board_action_failed"]
    assert "board_action_failed" not in rows["p2"]
    assert len(board.comments) == 1
    assert board.comments[0][0] == "p2"


def test_a_failed_invalid_comment_retries_next_pass_not_silenced_forever(monkeypatch):
    """The sharper wrinkle the same finding named: invalid_commented used to
    be stamped True BEFORE the comment was attempted, so a failed comment
    was silenced forever. It must now retry."""
    board = FakeBoard({"p1": _card(DeployComponent="", Name="Bad card")},
                      fail_comment_for={"p1"})
    result1 = dr.check_deployed(board, max_cards=10)
    assert result1["still_waiting"][0]["board_action_failed"]
    assert not board.comments

    result2 = dr.check_deployed(board, max_cards=10)
    assert "board_action_failed" not in result2["still_waiting"][0]
    assert len(board.comments) == 1


def test_deployed_with_qa_done_moves_straight_to_done(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["action"] == "moved_done"
    assert board.moves == [("p1", "done")]
    assert board.comments


def test_deployed_without_qa_done_moves_to_testing(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["action"] == "moved_testing"
    assert result["moved"][0]["qa_dispatch_needed"] is True
    assert board.moves == [("p1", "Testing")]


def test_qa_dispatch_needed_survives_a_rerun_until_marked_dispatched(monkeypatch):
    """Code-review finding on PR #23: the original version only attempted a
    QA dispatch on the pass that moved the card to Testing. A card already
    sitting in Testing from a PRIOR pass (because that dispatch failed) must
    still be reported as needing one on every subsequent pass, until the
    caller confirms success via mark_qa_dispatched()."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="")})
    dr.check_deployed(board, max_cards=10)  # moves to Testing
    assert board.moves == [("p1", "Testing")]

    result2 = dr.check_deployed(board, max_cards=10)  # QA dispatch "failed" — never marked
    row2 = result2["still_waiting"][0]
    assert row2["status"] == "Testing"
    assert row2["qa_dispatch_needed"] is True
    assert len(board.moves) == 1, "no second move — still a no-op on Status"


def test_mark_qa_dispatched_clears_the_flag(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="")})
    dr.check_deployed(board, max_cards=10)  # moves to Testing, qa_dispatch_needed=True

    dr.mark_qa_dispatched(board, "p1")

    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["qa_dispatch_needed"] is False


def test_qa_dispatch_not_needed_when_qa_already_done(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["action"] == "moved_done"
    assert result["moved"][0]["qa_dispatch_needed"] is False


def test_last_seen_sort_key_handles_mixed_and_missing_values():
    """Code-review finding on PR #23: max(hosts, key=lambda h: h.get(...) or
    0) raises TypeError the moment one host's last_seen_at is a non-numeric
    value and another's is missing/falsy — the int fallback 0 can't compare
    against a str. Every key must come back as a plain float."""
    assert dr._last_seen_sort_key({"last_seen_at": 100.5}) == 100.5
    assert dr._last_seen_sort_key({"last_seen_at": None}) == 0.0
    assert dr._last_seen_sort_key({}) == 0.0
    assert dr._last_seen_sort_key({"last_seen_at": "not-a-number"}) == 0.0
    hosts = [{"cli_version": "old", "last_seen_at": None},
             {"cli_version": "new", "last_seen_at": 200.0}]
    assert max(hosts, key=dr._last_seen_sort_key)["cli_version"] == "new"


def test_pending_stays_put_no_move_no_comment(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card()})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["verdict"] == "pending"
    assert not board.moves
    assert not board.comments


def test_unknown_never_moves_the_card(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    board = FakeBoard({"p1": _card()})
    for _ in range(2):
        result = dr.check_deployed(board, max_cards=10)
        assert result["still_waiting"][0]["verdict"] == "unknown"
        assert not board.moves


def test_three_consecutive_unknowns_escalate_to_need_human(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    board = FakeBoard({"p1": _card()})
    for _ in range(3):
        dr.check_deployed(board, max_cards=10)
    assert board.moves == [("p1", "need_human")]


def test_unknown_streak_clears_on_a_successful_observation(monkeypatch):
    board = FakeBoard({"p1": _card()})
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    dr.check_deployed(board, max_cards=10)
    dr.check_deployed(board, max_cards=10)
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.1.0", "error": None})
    dr.check_deployed(board, max_cards=10)  # resets the streak (pending verdict)
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    dr.check_deployed(board, max_cards=10)
    dr.check_deployed(board, max_cards=10)
    assert not board.moves, "streak should have reset, so this is only the 2nd unknown in a row"


def test_a_bare_timeout_on_one_cards_move_does_not_abort_the_whole_batch(monkeypatch):
    """The actual live root cause (2026-11-12), distinct from PR #25's already-
    fixed BoardUnavailable case: a response-level socket timeout from a real
    BoardClient escapes as a bare TimeoutError, not kanban_dispatch.
    BoardUnavailable (see test_kanban_dispatch.py's matching _request
    regression test). Before this fix, check_deployed()'s action-block guard
    caught only BoardUnavailable, so this exact exception type propagated out
    of the `for summary in candidates` loop and killed the whole pass with no
    result — exactly what happened live when board.move() hung on an
    escalation move and nothing caught the timeout that followed."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    _patch_observe(monkeypatch, "core", {"workspace": "aw", "observed": "v0.1.80",
                                         "error": None})
    board = FakeBoard({
        # Pre-seeded at streak=2 so this single call's 3rd-strike evaluation
        # reaches UNKNOWN_STREAK_LIMIT and calls board.move() directly,
        # without a prior pass moving p2 out of the Ready-to-Deploy lane
        # first (which would otherwise shrink "considered" on this call).
        "p1": _card(Name="Escalating card",
                   DeployCheckResult=json.dumps({"unknown_streak": 2})),
        "p2": _card(Name="Deployed card", DeployComponent="core",
                   DeployToVersion="v0.1.80", QAStatus="done"),
    }, fail_move_with_timeout_for={"p1"})

    result = dr.check_deployed(board, max_cards=10)

    assert result["ok"] is True
    assert result["considered"] == 2
    rows_by_pid = {r["page_id"]: r for r in result["still_waiting"] + result["moved"]}
    # p1's move() raised a bare TimeoutError, not BoardUnavailable — the loop
    # must still record it on the row rather than dying.
    assert rows_by_pid["p1"]["board_action_failed"]
    assert ("p1", "need_human") not in board.moves
    # p2, evaluated AFTER p1 in the same pass, must still have been reached
    # and moved — proof the batch survived p1's uncaught-before-this-fix
    # exception type instead of losing every remaining card's result.
    assert rows_by_pid["p2"]["action"] == "moved_done"
    assert board.moves == [("p2", "done")]


def test_rollback_after_a_prior_deployed_verdict_goes_to_need_human(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="")})  # not done -> moved_testing, stays put
    dr.check_deployed(board, max_cards=10)
    assert board.moves == [("p1", "Testing")]

    # Observed version drops back below target -> rollback.
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.0", "error": None})
    dr.check_deployed(board, max_cards=10)
    assert board.moves[-1] == ("p1", "need_human")


def test_rerun_with_no_change_is_a_no_op(monkeypatch):
    """Idempotent by construction: stamping happens every run, but a second
    run with an unchanged verdict writes no new comment/move."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card()})
    dr.check_deployed(board, max_cards=10)
    comments_after_1 = len(board.comments)
    moves_after_1 = len(board.moves)
    dr.check_deployed(board, max_cards=10)
    assert len(board.comments) == comments_after_1
    assert len(board.moves) == moves_after_1


def test_multi_workspace_partial_stays_put(monkeypatch):
    def fake_app(name, ws):
        return {"workspace": ws, "observed": "v0.35.1" if ws == "a" else "v0.1.0",
               "error": None}
    monkeypatch.setitem(dr.ADAPTERS, "app", fake_app)
    board = FakeBoard({"p1": _card(DeployWorkspaces=["a", "b"])})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "partial"
    assert not board.moves


# ── output contract ───────────────────────────────────────────────────────

def test_output_reports_moved_and_still_waiting_and_counts(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({
        "p1": _card(QAStatus="done"),
        "p2": _card(DeployComponent="", Name="Bad card"),
    })
    result = dr.check_deployed(board, max_cards=10)
    assert result["ok"] is True
    assert result["considered"] == 2
    assert len(result["moved"]) == 1
    assert len(result["still_waiting"]) == 1
    assert result["counts"]["moved_done"] == 1
    assert "errors" in result and result["errors"] == []
    assert result["continuation"] == {"has_more": False}


def test_truncation_is_explicit(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "x"})
    board = FakeBoard({f"p{i}": _card() for i in range(5)})
    result = dr.check_deployed(board, max_cards=2)
    assert result["truncated"] is True
    assert result["continuation"]["has_more"] is True
    assert result["considered"] == 2


def test_dry_run_writes_nothing(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, dry_run=True, max_cards=10)
    assert result["moved"] == []  # nothing actually moved, even though verdict is deployed
    assert not board.moves
    assert not board.writes
    assert not board.comments
