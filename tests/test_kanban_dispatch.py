"""The Kanban→run bridge.

The pure decision logic (what prompt, which endpoint, resumable or not) is
separated from the I/O in ``kanban_dispatch`` precisely so it can be tested
without a Notion token, an agents-platform, or a workspace API — that is what
this file exercises. The orchestration in ``mcp_server`` on top of it is thin
by design: skip, read, move, fire, stamp.

Run: python -m pytest tests/test_kanban_dispatch.py
"""
from __future__ import annotations

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
