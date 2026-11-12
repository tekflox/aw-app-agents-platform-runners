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

    def move(self, page_id, status, comment=""):
        if page_id in self._fail_move_with_timeout_for:
            self._fail_move_with_timeout_for.discard(page_id)
            raise TimeoutError("simulated response-level socket timeout")
        if status == "need_human" and not (comment or "").strip():
            # Mirrors aw-app-notion's real move_card() guard.
            raise dr.kanban_dispatch.BoardUnavailable(
                "status='need_human' requires a comment explaining the problem")
        self.moves.append((page_id, status))
        if comment:
            self.comments.append((page_id, comment))
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


def test_the_need_human_escalation_carries_its_comment_on_the_move_call(monkeypatch):
    """Live bug found 2026-11-12 while resuming the reconciliation pass this
    PR's own fix unblocked: aw-app-notion's move_card() requires a comment
    for status='need_human' and returns {"ok": False, ...} (not an
    exception) without one. The OLD code called board.add_comment() THEN
    board.move(pid, "need_human") with no comment — the comment posted, but
    every escalation move silently no-opped, while check_deployed() still
    reported action='needs_human' and a new_status of 'Need Human' as if it
    had worked. FakeBoard.move() mirrors the real guard (see move() above),
    so this test would fail the same way against the pre-fix code: it
    would raise BoardUnavailable from board.move(), get caught by the
    action-block guard, and board.moves would stay empty."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    board = FakeBoard({"p1": _card()})
    for _ in range(3):
        result = dr.check_deployed(board, max_cards=10)
    assert "board_action_failed" not in result["moved"][0]
    assert board.moves == [("p1", "need_human")]
    assert board.comments and "escalating" in board.comments[0][1]


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


# ── C3: inventory precedence (live > fresh inventory > agent evidence) ────

def _patch_inventory(monkeypatch, rows: dict[str, dict], error: str | None = None):
    monkeypatch.setattr(dr, "_inventory_fetch", lambda ws: (dict(rows), error))


def test_inventory_fetch_without_backend_url_is_a_clean_miss(monkeypatch):
    monkeypatch.delenv(dr._BACKEND_URL_VAR, raising=False)
    rows, error = dr._inventory_fetch("aw")
    assert rows == {}
    assert dr._BACKEND_URL_VAR in error


class _FakeHTTPResponse:
    """Minimal stand-in for ``http.client.HTTPResponse`` as a context
    manager — just enough for ``_http_get_json``'s ``with urlopen(...)``."""
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_inventory_fetch_handles_a_malformed_200_body_without_crashing(monkeypatch):
    """Code-review finding on PR #33 (reported twice): a 200 response with a
    non-JSON body raised json.JSONDecodeError uncaught, which — since
    _inventory_fetch is called unguarded at the top of check_deployed(),
    ahead of every per-card try/except — crashed the ENTIRE pass."""
    monkeypatch.setenv(dr._BACKEND_URL_VAR, "http://backend.example")
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=20.0: _FakeHTTPResponse(200, "not json"))
    rows, error = dr._inventory_fetch("aw")
    assert rows == {}
    assert "malformed" in error


def test_inventory_fetch_handles_a_wrongly_shaped_200_body_without_crashing(monkeypatch):
    """Same failure class, one layer deeper: valid JSON that isn't the
    expected {"components": [...]} shape (e.g. a bare JSON array) must not
    crash on the .get("components") call either."""
    monkeypatch.setenv(dr._BACKEND_URL_VAR, "http://backend.example")
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=20.0: _FakeHTTPResponse(200, "[1, 2, 3]"))
    rows, error = dr._inventory_fetch("aw")
    assert rows == {}
    assert "malformed" in error


def test_check_deployed_survives_a_malformed_inventory_body_end_to_end(monkeypatch):
    """The actual reported bug, reproduced through the real entry point:
    before the fix this raised out of check_deployed() itself and failed
    the whole kanban_check_deployed tool call for every card in the lane."""
    monkeypatch.setenv(dr._BACKEND_URL_VAR, "http://backend.example")
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=20.0: _FakeHTTPResponse(200, "not json"))
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["ok"] is True
    assert "malformed" in result["inventory_error"]
    assert result["moved"][0]["action"] == "moved_done"  # falls through to the live tier fine


def test_live_adapter_wins_even_over_a_fresh_inventory_row(monkeypatch):
    """'stale falls back to live' in spirit, but the stronger live-always-
    wins case: the live adapter is tried FIRST regardless of whether the
    inventory also has a (fresh) row — live asks the running thing right
    now, the inventory is a cache."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    _patch_inventory(monkeypatch, {"app:notion": {"version": "v0.1.0", "stale": False,
                                                  "age_s": 5}})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    row = result["moved"][0]
    assert row["observations"][0]["source"] == "live"
    assert row["observations"][0]["observed"] == "v0.35.1"
    assert row["action"] == "moved_done"  # v0.35.1 >= v0.35.1 target


def test_fresh_inventory_row_wins_when_live_adapter_fails(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    _patch_inventory(monkeypatch, {"app:notion": {"version": "v0.35.1", "stale": False,
                                                  "age_s": 12.0}})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    row = result["moved"][0]
    obs = row["observations"][0]
    assert obs["source"] == "inventory"
    assert obs["observed"] == "v0.35.1"
    assert obs["age_s"] == 12.0
    assert row["action"] == "moved_done"


def test_stale_inventory_row_is_skipped_falls_through(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    _patch_inventory(monkeypatch, {"app:notion": {"version": "v0.35.1", "stale": True,
                                                  "age_s": 999999}})
    board = FakeBoard({"p1": _card()})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "unknown"
    assert row["observations"][0]["source"] is None
    assert "stale" in row["observations"][0]["reason"]


def test_both_live_and_inventory_failing_is_unknown(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    _patch_inventory(monkeypatch, {}, error="backend unreachable")
    board = FakeBoard({"p1": _card()})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "unknown"
    assert row["observations"][0]["source"] is None


def test_fresh_agent_evidence_is_used_as_last_resort(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    _patch_inventory(monkeypatch, {})
    now = dr.time.time()
    recent = dr.dt.datetime.fromtimestamp(now - 60, tz=dr.dt.timezone.utc).isoformat()
    prior = json.dumps({"agent_observation": {"observed": "v0.35.1", "source": "docker inspect",
                                              "at": recent, "run_id": "r1"}})
    board = FakeBoard({"p1": _card(QAStatus="done", DeployCheckResult=prior)})
    result = dr.check_deployed(board, max_cards=10)
    row = result["moved"][0]
    obs = row["observations"][0]
    assert obs["source"] == "agent"
    assert obs["observed"] == "v0.35.1"
    assert row["action"] == "moved_done"


def test_agent_evidence_older_than_12h_is_ignored(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    _patch_inventory(monkeypatch, {})
    now = dr.time.time()
    old = dr.dt.datetime.fromtimestamp(now - 13 * 3600, tz=dr.dt.timezone.utc).isoformat()
    prior = json.dumps({"agent_observation": {"observed": "v0.35.1", "at": old}})
    board = FakeBoard({"p1": _card(DeployCheckResult=prior)})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "unknown"
    assert row["observations"][0]["source"] is None


def test_agent_evidence_is_scoped_to_the_workspace_it_was_recorded_for(monkeypatch):
    """Code-review finding on PR #33: a card's agent_observation is a single
    blob, not one per DeployWorkspaces entry — it must never be applied to
    a workspace other than the one it actually names."""
    def fake_app(name, ws):
        return {"workspace": ws, "observed": None, "error": "down"}
    monkeypatch.setitem(dr.ADAPTERS, "app", fake_app)
    _patch_inventory(monkeypatch, {})
    now = dr.time.time()
    recent = dr.dt.datetime.fromtimestamp(now - 60, tz=dr.dt.timezone.utc).isoformat()
    prior = json.dumps({"agent_observation": {"observed": "v0.35.1", "workspace": "a",
                                              "at": recent}})
    board = FakeBoard({"p1": _card(DeployWorkspaces=["b"], DeployCheckResult=prior)})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    obs = row["observations"][0]
    assert obs["workspace"] == "b"
    assert obs["source"] is None  # the 'a'-scoped evidence must not decide 'b'
    assert row["verdict"] == "unknown"


def test_agent_evidence_applies_only_to_its_matching_workspace_on_a_multi_ws_card(monkeypatch):
    def fake_app(name, ws):
        return {"workspace": ws, "observed": None, "error": "down"}
    monkeypatch.setitem(dr.ADAPTERS, "app", fake_app)
    _patch_inventory(monkeypatch, {})
    now = dr.time.time()
    recent = dr.dt.datetime.fromtimestamp(now - 60, tz=dr.dt.timezone.utc).isoformat()
    prior = json.dumps({"agent_observation": {"observed": "v0.35.1", "workspace": "b",
                                              "at": recent}})
    board = FakeBoard({"p1": _card(DeployWorkspaces=["a", "b"], DeployCheckResult=prior)})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    obs_by_ws = {o["workspace"]: o for o in row["observations"]}
    assert obs_by_ws["b"]["source"] == "agent"
    assert obs_by_ws["a"]["source"] is None


def test_agent_evidence_without_a_workspace_field_only_applies_to_this_workspace(monkeypatch):
    """A workspace-less agent_observation (the shape a single-workspace card
    naturally produces) is treated as being about this app's own workspace
    — the only one such a card ever resolves — never about another one."""
    def fake_app(name, ws):
        return {"workspace": ws, "observed": None, "error": "down"}
    monkeypatch.setitem(dr.ADAPTERS, "app", fake_app)
    _patch_inventory(monkeypatch, {})
    now = dr.time.time()
    recent = dr.dt.datetime.fromtimestamp(now - 60, tz=dr.dt.timezone.utc).isoformat()
    prior = json.dumps({"agent_observation": {"observed": "v0.35.1", "at": recent}})
    board = FakeBoard({"p1": _card(DeployWorkspaces=["other-ws"], DeployCheckResult=prior)})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["observations"][0]["source"] is None


def test_parse_iso_datetime_rejects_date_only_and_garbage():
    """Architect risk #2: a date-only value must not be mistaken for fresh."""
    assert dr._parse_iso_datetime(None) is None
    assert dr._parse_iso_datetime("") is None
    assert dr._parse_iso_datetime("2026-11-12") is None
    assert dr._parse_iso_datetime("not-a-date") is None
    assert dr._parse_iso_datetime("2026-11-12T18:51:00Z") is not None
    assert dr._parse_iso_datetime("2026-11-12T18:51:00+00:00") is not None


def test_evidence_names_the_source_used(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None,
                                        "error": "unreachable"})
    _patch_inventory(monkeypatch, {"app:notion": {"version": "v0.1.0", "stale": False,
                                                  "age_s": 30}})
    board = FakeBoard({"p1": _card()})  # pending: v0.1.0 < v0.35.1 target
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "pending"
    assert "source=inventory" in dr._evidence(row)
    assert "age=30s" in dr._evidence(row)


# ── C3: DeployComponent='none' sentinel ────────────────────────────────────

def test_none_sentinel_with_qa_done_moves_straight_to_done(monkeypatch):
    board = FakeBoard({"p1": _card(DeployComponent="none", DeployToVersion="", QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["verdict"] == "no_deploy_needed"
    assert result["moved"][0]["action"] == "moved_done"
    assert board.moves == [("p1", "done")]


def test_none_sentinel_without_qa_done_moves_to_testing_and_needs_qa(monkeypatch):
    board = FakeBoard({"p1": _card(DeployComponent="none", DeployToVersion="", QAStatus="")})
    result = dr.check_deployed(board, max_cards=10)
    row = result["moved"][0]
    assert row["verdict"] == "no_deploy_needed"
    assert row["action"] == "moved_testing"
    assert row["qa_dispatch_needed"] is True
    assert board.moves == [("p1", "Testing")]


def test_none_sentinel_never_requires_a_deploy_to_version(monkeypatch):
    """An empty DeployToVersion would normally be flagged 'invalid' —
    the sentinel is the one case that's eligible without one."""
    board = FakeBoard({"p1": _card(DeployComponent="none", DeployToVersion="")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["verdict"] == "no_deploy_needed"
    assert "reasons" not in result["moved"][0]


def test_none_sentinel_is_case_insensitive(monkeypatch):
    board = FakeBoard({"p1": _card(DeployComponent="None", DeployToVersion="", QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["verdict"] == "no_deploy_needed"


def test_none_sentinel_qa_dispatch_flag_survives_until_marked(monkeypatch):
    board = FakeBoard({"p1": _card(DeployComponent="none", DeployToVersion="", QAStatus="")})
    dr.check_deployed(board, max_cards=10)  # moves to Testing
    dr.mark_qa_dispatched(board, "p1")
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["qa_dispatch_needed"] is False


# ── is_live=true override (card 4165bf3b-9510-81ea, 2026-11-12 comment) ─

def test_is_live_true_moves_a_need_human_card_directly_to_done(monkeypatch):
    """The deterministic cleanup rule Frederico added to the card: if the
    reconciler inspects a card and is_live is true, it moves to Done no
    matter what QAStatus or Status says — Need Human included. Reached via
    page_id (as check_deployed already supports for exactly-this-card
    checks) since Need Human is not in DEPLOY_STATUSES' own sweep."""
    board = FakeBoard({"p1": _card(Status="Need Human", QAStatus="", is_live=True,
                                   DeployComponent="", DeployToVersion="")})
    result = dr.check_deployed(board, page_id="p1")
    assert result["moved"][0]["verdict"] == "live_confirmed"
    assert result["moved"][0]["action"] == "moved_done"
    assert board.moves == [("p1", "done")]


def test_is_live_true_overrides_a_not_done_qa_status_in_the_normal_sweep(monkeypatch):
    """Same override, reached through the ordinary Ready to Deploy/Testing
    sweep this time — QAStatus is deliberately not 'done', which would
    otherwise route to Testing, not Done."""
    board = FakeBoard({"p1": _card(Status="Testing", QAStatus="in_progress", is_live=True)})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["verdict"] == "live_confirmed"
    assert result["moved"][0]["action"] == "moved_done"
    assert board.moves == [("p1", "done")]


def test_is_live_true_skips_validation_entirely(monkeypatch):
    """Bypasses _validate() — a stale/cleanup card may have no valid
    DeployComponent/DeployToVersion left on it at all; that must not turn
    into an 'invalid' verdict instead of the override."""
    board = FakeBoard({"p1": _card(Status="Ready to Deploy", DeployComponent="",
                                   DeployToVersion="", is_live=True)})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0]["verdict"] == "live_confirmed"
    assert not board.comments[0][1].startswith("🤖 kanban_check_deployed: invalid")


def test_is_live_true_already_done_is_an_idempotent_noop(monkeypatch):
    """Re-running on a card already moved to Done must not re-comment or
    re-move it — same idempotency contract as every other verdict branch."""
    board = FakeBoard({"p1": _card(Status="Done", is_live=True)})
    result = dr.check_deployed(board, page_id="p1")
    assert result["still_waiting"][0]["verdict"] == "live_confirmed"
    assert result["still_waiting"][0]["action"] == "none"
    assert not board.moves
    assert not board.comments


def test_is_live_true_rerun_after_moving_is_also_a_noop(monkeypatch):
    """Two consecutive check_deployed() passes over the same is_live card:
    the first moves it to Done, so the second sweep (scoped to Ready to
    Deploy/Testing, same as every other verdict) no longer even considers
    it — and a direct page_id re-check confirms no further action."""
    board = FakeBoard({"p1": _card(Status="Testing", QAStatus="", is_live=True)})
    dr.check_deployed(board, max_cards=10)
    assert board.moves == [("p1", "done")]
    result2 = dr.check_deployed(board, max_cards=10)
    assert result2["considered"] == 0, "card left both lanes once moved to Done"
    result3 = dr.check_deployed(board, page_id="p1")
    assert result3["still_waiting"][0]["action"] == "none"
    assert board.moves == [("p1", "done")], "no second move"
    assert len(board.comments) == 1, "no second comment"


def test_is_live_false_or_missing_does_not_short_circuit(monkeypatch):
    """Sanity check the override is opt-in, not the new default path: a
    card with no is_live set (or explicitly False) still runs the normal
    validate/observe pipeline."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card(is_live=False), "p2": _card()})
    result = dr.check_deployed(board, max_cards=10)
    verdicts = {r["page_id"]: r["verdict"] for r in result["still_waiting"]}
    assert verdicts["p1"] == "pending"
    assert verdicts["p2"] == "pending"


# ── C4: DEPLOY_ACTIONS registry ──────────────────────────────────────────

def test_deploy_action_text_known_components_are_mapped_verbatim():
    assert dr.deploy_action_text("core", "v0.1.80") == dr.DEPLOY_ACTIONS["core"]
    assert dr.deploy_action_text("remote-host", "v1.2.3") == dr.DEPLOY_ACTIONS["remote-host"]
    assert (dr.deploy_action_text("service:aw-backend", "abc1234")
           == dr.DEPLOY_ACTIONS["service:aw-backend"])


def test_deploy_action_text_app_components_share_one_default():
    assert dr.deploy_action_text("app:notion", "v0.38.0") == dr._APP_DEFAULT_ACTION
    assert dr.deploy_action_text("app:kb", "v1.0.0") == dr._APP_DEFAULT_ACTION


def test_deploy_action_text_unmapped_component_is_generic_and_names_version():
    text = dr.deploy_action_text("service:some-new-thing", "v2.0.0")
    assert "service:some-new-thing" in text
    assert "v2.0.0" in text
    assert "DEPLOY_ACTIONS" in text  # nudges the reader to extend the registry


# ── C4: reminder-due detection on pending/partial cards ─────────────────

def test_pending_card_with_no_prior_reminder_is_due(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card()})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "pending"
    assert row["reminder_due"] is True
    assert row["reminder_action"] == dr._APP_DEFAULT_ACTION


def test_pending_card_reminded_recently_is_not_due_again(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    now = dr.dt.datetime.now(dr.dt.timezone.utc)
    recent = (now - dr.dt.timedelta(hours=1)).isoformat()
    board = FakeBoard({"p1": _card(LastDeployReminderAt=recent)})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["reminder_due"] is False
    assert result["still_waiting"][0]["reminder_action"] is None


def test_pending_card_reminded_over_12h_ago_is_due_again(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    now = dr.dt.datetime.now(dr.dt.timezone.utc)
    stale = (now - dr.dt.timedelta(hours=13)).isoformat()
    board = FakeBoard({"p1": _card(LastDeployReminderAt=stale)})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["reminder_due"] is True


def test_date_only_last_reminder_at_is_treated_as_stale_not_fresh(monkeypatch):
    """Mirrors the architect's risk #2 for agent evidence: a date-only
    value (_parse_iso_datetime rejects it) must never read as a fresh
    timestamp — it must fire the reminder again, not silently suppress it
    forever."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card(LastDeployReminderAt="2026-11-12")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["reminder_due"] is True


def test_partial_verdict_is_also_reminder_due(monkeypatch):
    def fake_app(name, ws):
        return {"workspace": ws, "observed": "v0.35.1" if ws == "a" else "v0.1.0", "error": None}
    monkeypatch.setitem(dr.ADAPTERS, "app", fake_app)
    board = FakeBoard({"p1": _card(DeployWorkspaces=["a", "b"])})
    result = dr.check_deployed(board, max_cards=10)
    row = result["still_waiting"][0]
    assert row["verdict"] == "partial"
    assert row["reminder_due"] is True


def test_deployed_card_is_never_reminder_due(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="done")})
    result = dr.check_deployed(board, max_cards=10)
    assert result["moved"][0].get("reminder_due") is False


def test_invalid_card_is_never_reminder_due():
    board = FakeBoard({"p1": _card(DeployComponent="")})
    result = dr.check_deployed(board, max_cards=10)
    assert "reminder_due" not in result["still_waiting"][0]


def test_unknown_card_is_never_reminder_due(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": None, "error": "down"})
    board = FakeBoard({"p1": _card()})
    result = dr.check_deployed(board, max_cards=10)
    assert result["still_waiting"][0]["reminder_due"] is False


def test_rolled_back_card_is_never_reminder_due(monkeypatch):
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.1", "error": None})
    board = FakeBoard({"p1": _card(QAStatus="")})
    dr.check_deployed(board, max_cards=10)  # moves to Testing
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.35.0", "error": None})
    result = dr.check_deployed(board, max_cards=10)  # rollback
    row = result["moved"][0]
    assert row["verdict"] == "rolled_back"
    assert row["reminder_due"] is False


def test_no_deploy_needed_sentinel_is_never_reminder_due():
    board = FakeBoard({"p1": _card(DeployComponent="none", DeployToVersion="", QAStatus="")})
    result = dr.check_deployed(board, max_cards=10)
    assert "reminder_due" not in result["moved"][0]


def test_rerun_after_a_sent_reminder_is_not_due_again_within_12h(monkeypatch):
    """Idempotency: once the caller confirms the stamp (mark_reminder_sent),
    the very next pass must not re-flag the same card as due."""
    _patch_observe(monkeypatch, "app", {"workspace": "aw", "observed": "v0.34.0", "error": None})
    board = FakeBoard({"p1": _card()})
    result1 = dr.check_deployed(board, max_cards=10)
    assert result1["still_waiting"][0]["reminder_due"] is True

    now_iso = dr.dt.datetime.now(dr.dt.timezone.utc).isoformat()
    dr.mark_reminder_sent(board, "p1", now_iso)

    result2 = dr.check_deployed(board, max_cards=10)
    assert result2["still_waiting"][0]["reminder_due"] is False


# ── C4: compose_reminder_message / send_batched_reminders ────────────────

def test_compose_reminder_message_is_one_message_for_every_row():
    rows = [
        {"title": "Card A", "component": "app:notion", "to_version": "v0.38.0",
         "verdict": "pending", "reminder_action": "marketplace release..."},
        {"title": "Card B", "component": "core", "to_version": "v0.1.80",
         "verdict": "partial", "reminder_action": "deploy workspace core"},
    ]
    title, text = dr.compose_reminder_message(rows)
    assert "2 card(s)" in title
    assert "Card A" in text and "app:notion" in text
    assert "Card B" in text and "deploy workspace core" in text
    assert text.count("\n") == 1  # exactly two lines, one per card


def test_send_batched_reminders_with_no_due_cards_sends_nothing():
    board = FakeBoard({})
    calls = []
    result = dr.send_batched_reminders(board, [], lambda title, text: calls.append((title, text)))
    assert result == {"sent": False, "count": 0}
    assert not calls
    assert not board.writes


def test_stamp_happens_before_send_not_after():
    """Deliberately the OPPOSITE ordering from mark_qa_dispatched/
    mark_invalid_commented (which stamp only after a confirmed success): a
    lost reminder self-heals in 12h, a spammed human does not (architect
    decision, card 4165bf3b-9510-812c). Pinned here so a future edit that
    reorders stamp-vs-send reproduces the exact spam this bias exists to
    prevent, instead of silently passing."""
    board = FakeBoard({"p1": _card(), "p2": _card(Name="Other")})
    rows = [
        {"page_id": "p1", "title": "Deploy X", "component": "app:notion",
         "to_version": "v0.35.1", "verdict": "pending", "reminder_action": "a"},
        {"page_id": "p2", "title": "Other", "component": "core",
         "to_version": "v0.2.0", "verdict": "pending", "reminder_action": "b"},
    ]
    stamped_pages_seen_by_send = []

    def fake_send(title, text):
        stamped_pages_seen_by_send.append(
            {pid for pid, name, _ in board.writes if name == "LastDeployReminderAt"})
        return {"ok": True}

    result = dr.send_batched_reminders(board, rows, fake_send)
    assert result == {"sent": True, "count": 2, "title": dr.compose_reminder_message(rows)[0]}
    assert len(board.writes) == 2
    # Both stamps were already on the board by the time send() ran.
    assert stamped_pages_seen_by_send == [{"p1", "p2"}]


def test_both_stamps_share_one_instant_for_the_whole_batch():
    board = FakeBoard({"p1": _card(), "p2": _card(Name="Other")})
    rows = [
        {"page_id": "p1", "title": "A", "component": "app:notion", "to_version": "v1",
         "verdict": "pending", "reminder_action": "a"},
        {"page_id": "p2", "title": "B", "component": "core", "to_version": "v2",
         "verdict": "pending", "reminder_action": "b"},
    ]
    dr.send_batched_reminders(board, rows, lambda t, x: {"ok": True})
    stamps = {pid: value for pid, name, value in board.writes if name == "LastDeployReminderAt"}
    assert stamps["p1"] == stamps["p2"]


def test_send_failure_leaves_the_stamp_in_place_self_heals_in_12h():
    board = FakeBoard({"p1": _card()})
    rows = [{"page_id": "p1", "title": "X", "component": "app:notion", "to_version": "v0.35.1",
            "verdict": "pending", "reminder_action": "a"}]

    def failing_send(title, text):
        raise RuntimeError("telegram unreachable")

    result = dr.send_batched_reminders(board, rows, failing_send)
    assert result["sent"] is False
    assert result["count"] == 1
    assert "telegram unreachable" in result["error"]
    assert len(board.writes) == 1, "the stamp already landed and is not rolled back"


def test_mark_reminder_sent_writes_the_real_property_not_the_json_blob():
    board = FakeBoard({"p1": _card()})
    dr.mark_reminder_sent(board, "p1", "2026-11-12T19:00:00+00:00")
    assert board.writes == [("p1", "LastDeployReminderAt", "2026-11-12T19:00:00+00:00")]
