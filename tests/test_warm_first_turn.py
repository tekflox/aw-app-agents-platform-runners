"""Turn 1 of a session used to be cold by construction: a warm container is
keyed by session_id, and the caller has none to send until claude invents one
on that first turn. Measured live on 2026-08-14, that was 39% of all
dispatches (214 of 545 in .tmp/execute_debug.log) — every new conversation
paying a full container spawn on a runner whose whole point is warm reuse.

The fix mints the session id in the runner instead and creates the session
with `--session-id <uuid>` rather than resuming one that does not exist yet.
These tests pin both halves: WHEN we mint, and WHICH flag the minted id gets.

Run: .venv/aw/bin/python -m pytest tests/test_warm_first_turn.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import warm_pool  # noqa: E402

BASE_JOB = {
    "run_id": "r1",
    "cli": "claude",
    "agent_id": "agent-1",
    "prompt": "hi",
}


def _warm_argv(job: dict, tmp_path: Path, monkeypatch) -> list[str]:
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    _image, kwargs = execute_mod._build_warm_kwargs(job, "epoch1", "redis://example.test:6379/0")
    return kwargs["command"]


# --------------------------------------------------------------------------
# Which flag a session id is handed over with
# --------------------------------------------------------------------------

def test_minted_session_is_created_not_resumed(tmp_path, monkeypatch):
    """`--resume <uuid>` on an id claude has never seen fails outright ("no
    conversation found"), which would make every first turn a hard error
    instead of merely a cold one."""
    argv = _warm_argv({**BASE_JOB, "session_id": "new-uuid",
                       "_warm_minted_session": True}, tmp_path, monkeypatch)
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "new-uuid"
    assert "--resume" not in argv


def test_caller_supplied_session_is_resumed(tmp_path, monkeypatch):
    """Turn 2+ arrives with the id agents-platform read back off the stream —
    that session really exists, so it must resume, not be re-created (which
    would silently start the conversation over with no history)."""
    argv = _warm_argv({**BASE_JOB, "session_id": "existing-uuid"}, tmp_path, monkeypatch)
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "existing-uuid"
    assert "--session-id" not in argv


def test_minted_session_keeps_the_rest_of_the_warm_argv(tmp_path, monkeypatch):
    """The mint path must not become a second, weaker argv builder — the
    2026-08-11 permission-gate bug came from exactly that divergence."""
    argv = _warm_argv({**BASE_JOB, "session_id": "new-uuid",
                       "_warm_minted_session": True,
                       "append_system_prompt": "extra rules"}, tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" in argv
    assert "--append-system-prompt" in argv
    assert "--input-format" in argv


# --------------------------------------------------------------------------
# When mint_warm_session_id mints at all
# --------------------------------------------------------------------------

@pytest.fixture
def warm_on(monkeypatch):
    monkeypatch.setattr(warm_pool, "enabled", lambda: True)


def test_first_turn_mints_a_session(warm_on):
    job = {**BASE_JOB, "session_id": None}
    assert execute_mod.mint_warm_session_id(job) is True
    assert job["session_id"], "warm dispatch would have no session id"
    assert job["_warm_minted_session"] is True


def test_minted_session_is_a_distinct_uuid_per_run(warm_on):
    seen = set()
    for run_id in ("r1", "r2", "r3"):
        job = {**BASE_JOB, "run_id": run_id, "session_id": None}
        execute_mod.mint_warm_session_id(job)
        seen.add(job["session_id"])
    assert len(seen) == 3, "two different conversations would share one warm container"


def test_existing_session_is_never_re_minted(warm_on):
    job = {**BASE_JOB, "session_id": "existing-uuid"}
    assert execute_mod.mint_warm_session_id(job) is False
    assert job["session_id"] == "existing-uuid"
    assert "_warm_minted_session" not in job


def test_no_mint_without_agent_id(warm_on):
    """agent_id is the other half of the container name. Minting a session id
    without one would just produce `aw-warm-None-<uuid>` — one shared bucket
    for every agent."""
    job = {**BASE_JOB, "agent_id": None, "session_id": None}
    assert execute_mod.mint_warm_session_id(job) is False
    assert not job["session_id"]


def test_no_mint_for_codex(warm_on):
    """Warm mode is claude-only; a minted claude uuid means nothing to codex,
    and its own resume story is separate (see execute.py's codex rollout
    comments)."""
    job = {**BASE_JOB, "cli": "codex", "session_id": None}
    assert execute_mod.mint_warm_session_id(job) is False
    assert not job["session_id"]


def test_no_mint_when_warm_is_switched_off(monkeypatch):
    """With warm_container unticked the runner must behave exactly as the
    pure ephemeral path did — including leaving session_id alone."""
    monkeypatch.setattr(warm_pool, "enabled", lambda: False)
    job = {**BASE_JOB, "session_id": None}
    assert execute_mod.mint_warm_session_id(job) is False
    assert not job["session_id"]


def test_default_cli_is_treated_as_claude(warm_on):
    """agents-platform omits `cli` for its default runner — execute.py's own
    `job.get("cli") or "claude"` convention, which this must not diverge
    from or first turns silently stay cold for the commonest caller."""
    job = {k: v for k, v in BASE_JOB.items() if k != "cli"}
    job["session_id"] = None
    assert execute_mod.mint_warm_session_id(job) is True
