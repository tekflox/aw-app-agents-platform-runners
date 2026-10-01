"""What execute.py's warm branch does when the pool ceiling is hit.

The ceiling is only half a feature. `get_or_create` raising `WarmPoolFull`
has to become a COLD RUN, not a failed one — otherwise the ceiling just
converts the incident's "228 containers then every spawn fails" into "40
containers then every spawn fails", which is the same outage with a smaller
memory footprint.

Before this, the warm branch was one `except Exception` that published
`spawn_error` and returned. That generic handler is load-bearing for every
other failure and is deliberately left exactly as it was; `WarmPoolFull` is
caught ahead of it and falls through to the cold/ephemeral path below, which
predates warm mode and auto-removes its containers.

Two things in that fall-through are easy to get wrong and are pinned here:

* **the redis client.** The warm branch closed `r` in a `finally`. The cold
  path still needs it (and closes it itself on each of its own exits), so a
  fall-through that kept the old `finally` would hand the cold path a closed
  connection.
* **`new_session`.** A job that reached the warm branch through
  `mint_warm_session_id()` has a `session_id` that does not exist yet, but
  records that ONLY as `_warm_minted_session`. The cold path picks
  `--session-id` vs `--resume` off `new_session` alone. Falling back
  without translating the one into the other means `--resume` on an id
  claude has never seen: empty reply, exit 0, recorded as a zero-token
  SUCCESS. That is the 2026-08-19 bug tests/test_cold_new_session.py exists
  for, reached by a new route.

Card: 3ec5bf3b-9510-8106-91f6-d9b31722daa4
Run: python3 -m pytest tests/test_warm_pool_full_fallback.py
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

# Grabbed before the harness fixture stubs it out, for the one test that
# wants the REAL argv builder behind its stub.
_REAL_BUILD_CONTAINER_KWARGS = execute_mod._build_container_kwargs


class _ReachedColdPath(Exception):
    """Raised from the stubbed cold builder purely to stop the test there —
    everything this file asserts has already happened by that point."""


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Drive _run_job_blocking as far as the warm/cold fork and no further."""
    state = {"cold_job": None, "published": [], "closed": 0}

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

    # `_run_job_blocking` does a function-local `import docker as docker_sdk`,
    # so a fake MODULE is what it picks up. The real SDK is deliberately not
    # imported here: it is a runtime dependency of the app, not of its test
    # environment, and importing it would make this file fail to collect
    # anywhere `docker` is not installed — including CI.
    fake_errors = types.ModuleType("docker.errors")
    fake_errors.APIError = type("APIError", (Exception,), {})
    fake_errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
    fake_docker = types.ModuleType("docker")
    fake_docker.DockerClient = lambda **kw: MagicMock()
    fake_docker.errors = fake_errors
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)

    def _cold(job, client=None):
        # The cold path's FIRST call. Capture the job as the cold path will
        # see it, then stop: spawning is not what this file is about.
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


def _spawn_errors(state) -> list:
    """Only the WARM branch's own spawn_error. The cold path publishes one
    too, and in these tests it always will — the stubbed builder raises to
    stop the run there — so matching on "spawn_error" alone would conflate
    the failure being asserted about with the test's own stopping mechanism.
    """
    return [line for line in state["published"] if "dispatch warm turn" in line]


# --------------------------------------------------------------------------
# WarmPoolFull -> cold
# --------------------------------------------------------------------------

def test_warm_pool_full_falls_through_to_the_cold_path(monkeypatch, harness):
    def _full(*a, **kw):
        raise warm_pool.WarmPoolFull("warm pool full (40/40) — not spawning aw-warm-x")

    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn", _full)
    _run(_job())

    assert harness["cold_job"] is not None, "the ceiling must not end the run"


def test_warm_pool_full_publishes_no_spawn_error(monkeypatch, harness):
    """The run is going to succeed cold — telling the caller it failed to
    spawn is precisely the behaviour being removed."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    _run(_job())

    assert _spawn_errors(harness) == []


def test_the_cold_path_still_gets_an_open_redis_client(harness, monkeypatch):
    """The warm branch's own `finally: r.close()` must not fire on the
    fall-through — the cold path publishes through that same client."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    _run(_job())

    assert harness["cold_job"] is not None
    assert harness["closed"] == 0, "redis was closed before the cold path could use it"


# --------------------------------------------------------------------------
# The minted-session trap
# --------------------------------------------------------------------------

def test_a_minted_session_falling_back_is_marked_new(monkeypatch, harness):
    """Without this the cold path emits `--resume <id claude never saw>`,
    which exits 0 with an empty reply and is recorded as a success."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    _run(_job(_warm_minted_session=True))

    assert harness["cold_job"]["new_session"] is True


def test_a_minted_session_produces_session_id_not_resume(monkeypatch, harness, tmp_path):
    """End to end into the real argv builder, so this cannot pass on a
    `new_session` flag that the cold path then ignores."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")

    captured = {}

    def _capture(job, client=None):
        captured["argv"] = _REAL_BUILD_CONTAINER_KWARGS(job)[1]
        raise _ReachedColdPath()

    monkeypatch.setattr(execute_mod, "_build_container_kwargs", _capture)
    _run(_job(_warm_minted_session=True))

    argv = captured["argv"]
    assert "--session-id" in argv and "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == SESSION


def test_a_real_session_falling_back_still_resumes(monkeypatch, harness):
    """The mirror image. Turn 2+ of a genuine conversation must NOT be
    turned into a fresh session by the fallback — that would silently
    restart the conversation empty, losing its whole history."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    _run(_job())  # no _warm_minted_session

    assert harness["cold_job"].get("new_session") is not True


def test_an_explicit_new_session_is_preserved(monkeypatch, harness):
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=warm_pool.WarmPoolFull("full")))
    _run(_job(new_session=True))

    assert harness["cold_job"]["new_session"] is True


# --------------------------------------------------------------------------
# Every OTHER failure keeps today's behaviour exactly
# --------------------------------------------------------------------------

def test_an_ordinary_warm_failure_still_publishes_spawn_error(monkeypatch, harness):
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=RuntimeError("podman exploded")))
    _run(_job())

    assert len(_spawn_errors(harness)) == 1
    assert "podman exploded" in _spawn_errors(harness)[0]


def test_an_ordinary_warm_failure_does_not_fall_through_to_cold(monkeypatch, harness):
    """The fall-through is for the ceiling ALONE. Retrying a genuinely
    broken dispatch cold would double every real failure's cost and hide
    the fault behind a slow success."""
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=RuntimeError("podman exploded")))
    _run(_job())

    assert harness["cold_job"] is None


def test_an_ordinary_warm_failure_still_closes_redis(monkeypatch, harness):
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn",
                        MagicMock(side_effect=RuntimeError("podman exploded")))
    _run(_job())

    assert harness["closed"] == 1


def test_a_successful_warm_dispatch_does_not_touch_the_cold_path(monkeypatch, harness):
    monkeypatch.setattr(execute_mod, "_dispatch_warm_turn", MagicMock())
    _run(_job())

    assert harness["cold_job"] is None
    assert _spawn_errors(harness) == []
    assert harness["closed"] == 1


# --------------------------------------------------------------------------
# A job that never qualified for warm is unaffected either way
# --------------------------------------------------------------------------

def test_a_job_without_a_session_id_goes_straight_to_cold(monkeypatch, harness):
    monkeypatch.setattr(warm_pool, "enabled", lambda: False)
    _run(_job(session_id=None))

    assert harness["cold_job"] is not None
