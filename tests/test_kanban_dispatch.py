"""The Kanban→run bridge.

The pure decision logic (what prompt, which endpoint, resumable or not) is
separated from the I/O in ``kanban_dispatch`` precisely so it can be tested
without a Notion token, an agents-platform, or a workspace API — that is what
this file exercises. The orchestration in ``mcp_server`` on top of it is thin
by design: skip, read, move, fire, stamp.

Run: python -m pytest tests/test_kanban_dispatch.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import kanban_dispatch as kd  # noqa: E402


def _card(**over):
    base = {"page_id": "page-1", "title": "Fix the thing",
            "agent_slug": "coder-sonnet", "workflow_slug": None,
            "target_slug": "some-target",
            "body_md": "The button does nothing on mobile.",
            "comments_md": "- **2026-08-01** — tried a CSS fix, no luck"}
    base.update(over)
    return base


# ── the prompt ──────────────────────────────────────────────────────────

def test_prompt_carries_the_body_and_the_comments():
    text = kd.build_run_input(_card())
    assert "The button does nothing on mobile." in text
    assert "tried a CSS fix" in text
    assert "page_id=page-1" in text
    # Without this the agent guesses at REST endpoints instead of calling the
    # tools it already has — the specific incident that put the line in.
    assert "aw-kanban" in text


def test_prompt_falls_back_to_the_title_when_the_card_has_no_body():
    text = kd.build_run_input(_card(body_md="", comments_md=""))
    assert "Run task: Fix the thing" in text
    assert "Comment history" not in text, "no comments means no empty section"


def test_prompt_orders_body_before_history():
    text = kd.build_run_input(_card())
    assert text.index("Task content") < text.index("Comment history")


# ── which endpoint ──────────────────────────────────────────────────────

def test_agent_card_dispatches_to_the_agent_endpoint():
    path, payload = kd.dispatch_payload(_card(), "INPUT")
    assert path == "/api/agents/coder-sonnet/run"
    assert payload["input"] == {"input": "INPUT"}
    assert payload["notion_task_id"] == "page-1"
    assert payload["target_slug"] == "some-target"


def test_workflow_card_dispatches_to_the_workflow_endpoint():
    path, _ = kd.dispatch_payload(
        _card(agent_slug=None, workflow_slug="spec-pipeline"), "INPUT")
    assert path == "/api/workflows/spec-pipeline/run"


def test_agent_wins_when_a_card_names_both():
    """Same precedence the monolith had. Worth pinning: a board where someone
    filled in both fields should behave predictably, not by dict ordering."""
    path, _ = kd.dispatch_payload(
        _card(agent_slug="coder-sonnet", workflow_slug="spec-pipeline"), "INPUT")
    assert path == "/api/agents/coder-sonnet/run"


def test_card_without_a_slug_is_not_dispatchable():
    assert kd.dispatch_payload(_card(agent_slug=None, workflow_slug=None), "X") is None


def test_card_without_a_target_gets_the_monolith_default():
    """Cards created before this port rely on it, so the default is behaviour,
    not a convenience."""
    _, payload = kd.dispatch_payload(_card(target_slug=""), "INPUT")
    assert payload["target_slug"] == kd.DEFAULT_TARGET_SLUG == "system-investigations"


# ── resume ──────────────────────────────────────────────────────────────

def test_resume_reuses_the_runs_own_agent_and_session():
    path, payload = kd.resume_payload(
        {"source_slug": "coder-sonnet", "session_id": "sess-1", "target_id": "t-1"},
        "page-1", "keep going")
    assert path == "/api/agents/coder-sonnet/run"
    assert payload["session_id"] == "sess-1"
    assert payload["input"] == {"input": "keep going"}
    assert payload["target_id"] == "t-1"
    assert payload["notion_task_id"] == "page-1"


def test_resume_omits_target_id_when_the_run_has_none():
    _, payload = kd.resume_payload(
        {"source_slug": "a", "session_id": "s"}, "page-1", "x")
    assert "target_id" not in payload


def test_a_run_with_no_session_explains_itself_instead_of_failing():
    """A run that never opened a CLI session is a real state — an agent asking
    why should get a sentence, not a stack trace."""
    reason = kd.resume_payload({"source_slug": "coder-sonnet"}, "page-1", "x")
    assert isinstance(reason, str)
    assert "session_id" in reason


def test_a_run_with_no_agent_explains_itself():
    reason = kd.resume_payload({"session_id": "s"}, "page-1", "x")
    assert isinstance(reason, str)
    assert "source_slug" in reason


# ── the board client's own failure surface ──────────────────────────────

def test_board_prefers_the_published_url_over_loopback(monkeypatch):
    """Both apps are `tier: inprocess`, which makes loopback look right and
    isn't: this module is imported by an MCP server the gateway spawns inside
    ITS OWN container, where 127.0.0.1 is the gateway. Loopback-first there
    doesn't fail loudly — it talks to the wrong server."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    assert kd.board_base_url() == "https://aw.example"


def test_board_falls_back_to_loopback_only_as_a_last_resort(monkeypatch):
    """Kept for running inside the server itself — tests, a REPL."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_API_URL", raising=False)
    monkeypatch.delenv("AW_PORT", raising=False)
    monkeypatch.setattr(kd, "_from_env_file", lambda _n: None)
    assert kd.board_base_url() == "http://127.0.0.1:9030"


def test_the_upstream_env_carries_the_workspace_credentials(monkeypatch):
    """The bug this pair exists to prevent: the tools shipped, registered, and
    returned 503 on the first real call because the gateway's stdio child
    inherits nothing from the workspace server."""
    from agents_platform_runners_app import plugin

    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k-123")
    env = plugin.build_mcp_servers({})["agents-platform-runners"]["env"]
    assert env["AW_WORKSPACE_API_URL"] == "https://aw.example"
    assert env["AW_WORKSPACE_API_KEY"] == "k-123"


def test_a_missing_credential_is_omitted_not_written_blank(monkeypatch):
    """A blank value would reach the board as a 401 nobody can place; absent,
    the upstream raises its own "not set" error naming where it comes from."""
    from agents_platform_runners_app import plugin

    monkeypatch.delenv("AW_WORKSPACE_API_KEY", raising=False)
    monkeypatch.setattr(plugin, "_workspace_env",
                        lambda n: "" if n == "AW_WORKSPACE_API_KEY" else "https://aw.example")
    env = plugin.build_mcp_servers({})["agents-platform-runners"]["env"]
    assert "AW_WORKSPACE_API_KEY" not in env
    assert env["AGENTS_BASE"]


def test_board_names_itself_when_it_cannot_be_reached(monkeypatch):
    import urllib.error

    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    monkeypatch.setattr(kd.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            urllib.error.URLError("Connection refused")))
    try:
        kd.BoardClient().ready_cards()
    except kd.BoardUnavailable as exc:
        assert "aw-app-notion" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable")


def test_a_404_is_reported_as_the_notion_app_being_absent(monkeypatch):
    """The likeliest real cause by far — this tool ships in an app that does
    not depend on aw-app-notion being installed."""
    import urllib.error

    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    def gone(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

    monkeypatch.setattr(kd.urllib.request, "urlopen", gone)
    try:
        kd.BoardClient().ready_cards()
    except kd.BoardUnavailable as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("expected BoardUnavailable")


def test_ready_cards_unwraps_the_boards_envelope(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")

    class R:
        def read(self): return b'{"count":1,"cards":[{"page_id":"p1"}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(kd.urllib.request, "urlopen", lambda *a, **kw: R())
    assert kd.BoardClient().ready_cards() == [{"page_id": "p1"}]


def test_card_asks_for_the_body_and_comments(monkeypatch):
    """The whole reason get_kanban_card grew those flags — a dispatch prompt
    without them is just the card's title."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k")
    seen = {}

    class R:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        return R()

    monkeypatch.setattr(kd.urllib.request, "urlopen", capture)
    kd.BoardClient().card("p1")
    assert "include_body=true" in seen["url"]
    assert "include_comments=true" in seen["url"]


def test_the_watchdog_addresses_the_board_over_loopback(monkeypatch):
    """The in-process watchdog runs INSIDE the workspace server, so the
    published URL sends its traffic out to the tunnel edge and back — and that
    edge cuts at ~30s, under this module's own 60s card-read timeout. A slow
    card read there dies at the edge, not at the timeout sized for it."""
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://aw.example")
    monkeypatch.setenv("AW_PORT", "9030")
    assert kd.board_base_url(prefer_loopback=True) == "http://127.0.0.1:9030"
    assert kd.board_base_url() == "https://aw.example", "the MCP path is unchanged"


# ── the claim ───────────────────────────────────────────────────────────
#
# The guard the whole cut-over rests on. Two dispatchers read two different run
# databases, so "is a run already in flight?" is blind across the fence — the
# card is the only shared signal, and a write-then-read-back on it is the only
# way to make exactly one side win.

class FakeBoard:
    """A board where AgentRunId is a single last-write-wins cell, which is what
    Notion's page patch actually is."""

    def __init__(self, *, steal_with: str | None = None, cards=(), card=None):
        self.props: dict[str, str] = {}
        self.steal_with = steal_with
        self.moves: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self._cards = list(cards)
        self._card = card or {}

    def ready_cards(self, status="ready", limit=50):
        return self._cards

    def card(self, page_id, *, with_content=True):
        return dict(self._card)

    def move(self, page_id, status):
        self.moves.append((page_id, status))
        return {"ok": True}

    def set_property(self, page_id, name, value):
        self.writes.append((name, value))
        self.props[name] = value
        return {"ok": True}

    def properties(self, page_id, names=None):
        if self.steal_with is not None:
            # The other side patched in between our write and our read-back.
            self.props[kd.RUN_ID_PROPERTY] = self.steal_with
            self.steal_with = None
        return dict(self.props)


def test_claim_won_returns_its_own_token():
    board = FakeBoard()
    token = kd.claim_card(board, "p1")
    assert token and token.startswith("mt:")
    assert board.props[kd.RUN_ID_PROPERTY] == token


def test_claim_lost_when_the_other_side_wrote_last():
    """Both sides patch, both read back, and the read returns ONE value — so
    the side that doesn't see its own token stands down. This is the case that
    a status check alone cannot catch: both GETs landed before either PATCH."""
    board = FakeBoard(steal_with="mono:someone-else")
    assert kd.claim_card(board, "p1") is None


def test_a_stale_read_fails_closed_rather_than_double_firing():
    """A read that returns something unrelated (empty, an old run id, a human's
    hand edit) makes us stand down. Both sides doing that leaves the card in
    Ready — visible on the board and reclaimed by the next tick — instead of
    two runs on one card."""
    assert kd.claim_card(FakeBoard(steal_with=""), "p1") is None
    assert kd.claim_card(FakeBoard(steal_with="run-from-yesterday"), "p1") is None


def test_the_claim_token_names_the_side_that_holds_it():
    """So a human looking at a card stuck mid-claim can tell who was holding
    the lease when it stopped."""
    assert kd.claim_card(FakeBoard(), "p1", side="mono").startswith("mono:")


# ── the sweep ───────────────────────────────────────────────────────────

class FakePlatform:
    def __init__(self, *, latest=None, run_id="run-1", error=""):
        self._latest = latest
        self._run_id = run_id
        self._error = error
        self.dispatched: list[tuple[str, dict]] = []

    async def latest_run(self, page_id):
        return self._latest

    async def dispatch(self, path, payload):
        self.dispatched.append((path, payload))
        return ("", self._error) if self._error else (self._run_id, "")


def _sweep(board, platform, **kw):
    return asyncio.run(kd.sweep_ready(board, platform, **kw))


def _ready_summary(**over):
    base = {"page_id": "p1", "title": "Fix the thing", "status": "Ready",
            "agent_slug": "coder-sonnet", "target_slug": "some-target"}
    base.update(over)
    return base


def test_sweep_claims_moves_dispatches_and_stamps_in_that_order():
    """Move BEFORE dispatch is the monolith's order and it is deliberate: a
    card left in Ready after a good dispatch gets swept again and looks like a
    duplicate; one moved after a failed dispatch just sits there silently."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready", "body_md": "do it"})
    platform = FakePlatform()
    out = _sweep(board, platform)

    assert out["dispatched"] == 1
    assert board.moves == [("p1", "running")]
    assert platform.dispatched[0][0] == "/api/agents/coder-sonnet/run"
    # First write is the claim token, last is the real run id.
    assert board.writes[0][1].startswith("mt:")
    assert board.writes[-1] == (kd.RUN_ID_PROPERTY, "run-1")


def test_sweep_stands_down_when_the_claim_is_lost():
    """Nothing is moved and nothing is dispatched — the other side won it."""
    board = FakeBoard(steal_with="mono:x", cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready"})
    platform = FakePlatform()
    out = _sweep(board, platform)

    assert out["dispatched"] == 0
    assert out["results"][0]["skipped"] == "claim-lost"
    assert board.moves == []
    assert platform.dispatched == []


def test_sweep_skips_a_card_with_an_in_flight_run_before_writing_anything():
    board = FakeBoard(cards=[_ready_summary()], card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(latest={"id": "r-9", "status": "running"}))

    assert out["results"][0]["skipped"] == "run-already-in-flight"
    assert board.writes == []


def test_sweep_skips_a_card_the_other_side_already_moved():
    """The re-read is what catches the monolith winning the race between our
    list and our claim: the card is no longer Ready, so it isn't ours."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "In Progress"})
    out = _sweep(board, FakePlatform())

    assert out["results"][0]["skipped"] == "no-longer-ready"
    assert board.writes == []


def test_sweep_skips_a_card_with_no_slug_without_claiming_it():
    """A card nobody finished filling in is `skipped`, not an error — and it
    must not burn a claim, or the monolith reads our token on a card we were
    never going to dispatch."""
    board = FakeBoard(cards=[_ready_summary(agent_slug=None)],
                      card={"page_id": "p1", "status": "Ready", "agent_slug": None})
    out = _sweep(board, FakePlatform())

    assert "no agent_slug" in out["results"][0]["skipped"]
    assert board.writes == [] and board.moves == []


def test_dry_run_writes_nothing_at_all():
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(), dry_run=True)

    assert out["results"][0]["would_dispatch"] == "/api/agents/coder-sonnet/run"
    assert board.writes == [] and board.moves == []


def test_one_bad_card_does_not_end_the_pass():
    """try/except per card. A board where one page 404s still dispatches the
    rest — the sweep is the only trigger once the webhook is off."""
    class Flaky(FakeBoard):
        def card(self, page_id, *, with_content=True):
            if page_id == "bad":
                raise kd.BoardUnavailable("aw-app-notion returned 404")
            return {"page_id": page_id, "status": "Ready"}

    board = Flaky(cards=[_ready_summary(page_id="bad"), _ready_summary(page_id="good")])
    out = _sweep(board, FakePlatform())

    assert "404" in out["results"][0]["error"]
    assert out["results"][1]["run_id"] == "run-1"
    assert out["dispatched"] == 1


def test_a_failed_dispatch_is_reported_not_swallowed():
    board = FakeBoard(cards=[_ready_summary()], card={"page_id": "p1", "status": "Ready"})
    out = _sweep(board, FakePlatform(error="dispatch failed: 500 boom"))

    assert out["dispatched"] == 0
    assert "500" in out["results"][0]["error"]


def test_the_prompt_still_gets_the_body_the_summary_lacks():
    """`list_cards` returns no body/comments; `card()` does. The merge is why
    the dispatched agent sees the task instead of just its title."""
    board = FakeBoard(cards=[_ready_summary()],
                      card={"page_id": "p1", "status": "Ready",
                            "body_md": "The button does nothing on mobile.",
                            "comments_md": "- tried a CSS fix"})
    platform = FakePlatform()
    _sweep(board, platform)

    sent = platform.dispatched[0][1]["input"]["input"]
    assert "does nothing on mobile" in sent and "tried a CSS fix" in sent
