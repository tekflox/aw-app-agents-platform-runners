"""`one_shot` — a caller-stated fact ("this job will never get a turn 2"),
not an order about runner internals: the runner honours it by never
creating warm capacity for the job, so it always takes the existing
cold/ephemeral path, which already self-removes.

Two independent guards, because there are two independent ways a job can
end up creating warm capacity:

* `mint_warm_session_id` (execute.py:2511) mints a fresh session_id for a
  caller that sent none, so turn 1 can go warm too — claude-only.
* the warm-dispatch gate inside `_run_job_blocking` (execute.py:2974)
  spawns/reuses a warm container for ANY job that already carries BOTH
  agent_id and session_id, regardless of who put the session_id there.

Gating mint alone would have missed the literature-review leftovers that
opened this card: those were CODEX containers, and `mint_warm_session_id`
is claude-only, so they could only have come through the second gate on a
job that already carried a session_id. Both guards are pinned here, plus
a mutation check on each — reverting either guard's `one_shot` clause must
turn a specific test red, or the guard could regress silently.

Card: 4035bf3b-9510-81c8-a62c-cff209d7e175
Run: /usr/bin/python3.12 -m pytest tests/test_one_shot_warm_skip.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import warm_pool  # noqa: E402

SESSION = "710ca728-30a6-4aed-b4ec-ce16815fde5d"

BASE_JOB = {
    "run_id": "r1",
    "cli": "claude",
    "agent_id": "agent-1",
    "prompt": "hi",
}


@pytest.fixture
def warm_on(monkeypatch):
    monkeypatch.setattr(warm_pool, "enabled", lambda: True)


# --------------------------------------------------------------------------
# (a) one_shot claude job, no session_id -> no mint, cold path
# --------------------------------------------------------------------------

def test_one_shot_job_is_never_minted_a_session(warm_on):
    """Mutation check for execute.py:2511's `one_shot` clause: delete it and
    this goes red, because a one_shot first turn would otherwise be minted
    a session and sent warm exactly like a real conversation's turn 1."""
    job = {**BASE_JOB, "session_id": None, "one_shot": True}
    assert execute_mod.mint_warm_session_id(job) is False
    assert not job.get("session_id")
    assert "_warm_minted_session" not in job


# --------------------------------------------------------------------------
# (c) flag absent -> mint still happens (regression guard)
# --------------------------------------------------------------------------

def test_absent_one_shot_still_mints_a_session(warm_on):
    """today's behaviour, untouched: an ordinary caller that never heard of
    `one_shot` must keep getting a warmable turn 1."""
    job = {**BASE_JOB, "session_id": None}
    assert execute_mod.mint_warm_session_id(job) is True
    assert job["session_id"]
    assert job["_warm_minted_session"] is True


def test_one_shot_false_still_mints_a_session(warm_on):
    """The explicit-false shape routes.py actually sends
    (`bool(body.get("one_shot"))`, never an absent key) must behave
    identically to the flag being absent."""
    job = {**BASE_JOB, "session_id": None, "one_shot": False}
    assert execute_mod.mint_warm_session_id(job) is True
    assert job["session_id"]


# --------------------------------------------------------------------------
# (b) one_shot job WITH session_id, warm enabled -> warm branch skipped,
# no warm_pool.get_or_create call, cold path taken
# --------------------------------------------------------------------------

class _ReachedColdPath(Exception):
    """Raised from the stubbed cold builder purely to stop the test there."""


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Drive _run_job_blocking as far as the warm/cold fork and no further —
    same shape as test_warm_pool_full_fallback.py's harness."""
    state = {"cold_job": None, "published": [], "closed": 0, "warm_dispatched": 0}

    redis_client = MagicMock()
    redis_client.close.side_effect = lambda: state.__setitem__("closed", state["closed"] + 1)
    monkeypatch.setattr(execute_mod, "_redis_client", lambda _url: redis_client)
    monkeypatch.setattr(execute_mod, "_publish_line",
                        lambda _r, run_id, line: state["published"].append(line))
    monkeypatch.setattr(execute_mod, "_publish_done", lambda *a, **kw: None)
    monkeypatch.setattr(execute_mod, "_forget_run", lambda *a, **kw: None)
    monkeypatch.setattr(execute_mod.execution_index, "start", lambda *a, **kw: None)
    monkeypatch.setattr(execute_mod.execution_index, "start_after_stream_done",
                        lambda *a, **kw: None)

    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", str(tmp_path / "fake.sock"))
    monkeypatch.setattr(warm_pool, "enabled", lambda: True)

    fake_errors = types.ModuleType("docker.errors")
    fake_errors.APIError = type("APIError", (Exception,), {})
    fake_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
    fake_docker = types.ModuleType("docker")
    fake_docker.DockerClient = lambda **kw: MagicMock()
    fake_docker.errors = fake_errors
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)

    def _warm(*a, **kw):
        state["warm_dispatched"] += 1

    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn", _warm)

    def _cold(job, client=None):
        state["cold_job"] = dict(job)
        raise _ReachedColdPath()

    monkeypatch.setattr(execute_mod, "_build_container_kwargs", _cold)
    return state


def _job(**over) -> dict:
    job = {"run_id": "r1", "cli": "claude", "agent_id": "agent-1",
           "prompt": "hi", "session_id": SESSION}
    job.update(over)
    return job


def _run(job):
    execute_mod._run_job_blocking(job, "redis://example.test:6379/0")


def test_one_shot_with_session_id_skips_the_warm_branch_entirely(harness):
    """Mutation check for execute.py:2974's `one_shot` clause: delete it and
    this goes red — a one_shot job that happens to carry a session_id (e.g.
    a resumed-but-stateless workflow node) would otherwise still spin up or
    reuse a warm container."""
    _run(_job(one_shot=True))

    assert harness["warm_dispatched"] == 0, "one_shot must never reach _dispatch_warm_turn"
    assert harness["cold_job"] is not None, "one_shot must fall through to the cold path"


def test_a_real_session_with_one_shot_absent_still_goes_warm(harness):
    """Baseline for the mutation check above: without `one_shot` the same
    job (claude, agent_id + session_id, warm enabled) goes warm exactly as
    it always has — proving the skip above is about the flag, not about
    something else in the harness."""
    _run(_job())

    assert harness["warm_dispatched"] == 1
    assert harness["cold_job"] is None


def test_one_shot_false_with_session_id_still_goes_warm(harness):
    """The explicit-false shape routes.py sends must behave like an
    ordinary session-carrying turn, not like one_shot=True."""
    _run(_job(one_shot=False))

    assert harness["warm_dispatched"] == 1
    assert harness["cold_job"] is None
