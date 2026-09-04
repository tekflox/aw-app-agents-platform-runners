"""_run_job_blocking's cold-path codex retry (execute.py's _stream_cold_attempt
and its calling loop) — the cold-path counterpart of
aw-warm-relay-codex.py's own _run_codex_turn.

Card 3d15bf3b-9510-8164-95c8-d1c26da0df00: codex's "no rollout found for
thread id ... (code -32600)" fires under concurrent load on the ONE shared
$CODEX_HOME even when the rollout and its state_5.sqlite index row are both
intact — confirmed against codex's own upstream source (state_5.sqlite gets
a hardcoded 5s busy_timeout, not configurable, no retry beyond it —
codex-rs/state/src/sqlite.rs — same contention class as open bugs
openai/codex#20213 and #35555). _stream_cold_attempt buffers only until it
can tell whether an attempt is doomed, exactly like the warm relay does, so
a retry never reaches the run's Redis stream.

Run: .venv/aw/bin/python -m pytest tests/test_execute_codex_rollout_retry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

ROLLOUT_ERROR = ("Error: thread/resume: thread/resume failed: no rollout "
                  "found for thread id 01a06cc7-c10e-7123-b9ac-ba1335398bcd "
                  "(code -32600)")


class _FakeRedis:
    def __init__(self):
        self.lines: list[str] = []


class _FakeContainer:
    """`.logs(stream=True, follow=True)` yields raw byte CHUNKS, not lines —
    _stream_cold_attempt does its own newline buffering. One chunk per line
    is the simplest faithful stand-in (matches how test_execute_raw_command.py
    and friends treat the docker-py container object as a bare test double)."""

    def __init__(self, lines: list[str], rc: int):
        self._chunks = [(l + "\n").encode() for l in lines]
        self._rc = rc

    def logs(self, stream=True, follow=True):
        return iter(self._chunks)

    def wait(self):
        return {"StatusCode": self._rc}


def _publish_capture(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(execute_mod, "_publish_line",
                         lambda _r, _run_id, line: lines.append(line))
    return lines


def test_ordinary_attempt_streams_live(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer(['{"type":"thread.started"}', '{"type":"turn.completed"}'], 0)
    rc, contention_hit = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", True)
    assert rc == 0
    assert contention_hit is False
    assert published == ['{"type":"thread.started"}', '{"type":"turn.completed"}']


def test_contention_hit_publishes_nothing_when_a_retry_follows(monkeypatch):
    """A doomed attempt the caller is about to retry must be invisible —
    nothing reaches the run's event stream, so a retry looks clean."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, contention_hit = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 1
    assert contention_hit is True
    assert published == []


def test_contention_hit_on_the_last_attempt_still_publishes(monkeypatch):
    """Once the caller has decided not to retry again, a withheld contention
    error must still surface — never silently swallowed (this is what
    test_retry_loop_gives_up_after_max_attempts exercises end to end via
    _run_cold_agent_with_retry; this pins the single-attempt contract it
    depends on)."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, contention_hit = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 1
    assert contention_hit is True
    assert published == [ROLLOUT_ERROR]


def test_non_codex_job_never_treats_it_as_retryable(monkeypatch):
    """codex_retryable=False (any non-codex CLI) must behave exactly like
    the loop did before retry support existed — publish immediately, no
    contention detection at all."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, contention_hit = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", False)
    assert rc == 1
    assert contention_hit is False
    assert published == [ROLLOUT_ERROR]


def test_signature_after_real_content_is_not_contention(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer(['{"type":"thread.started"}', ROLLOUT_ERROR], 0)
    rc, contention_hit = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", True)
    assert rc == 0
    assert contention_hit is False
    assert published == ['{"type":"thread.started"}', ROLLOUT_ERROR]


def test_retry_loop_respawns_and_hides_failed_attempts(monkeypatch):
    """End-to-end over the real _run_cold_agent_with_retry: two contention
    hits then a clean attempt must respawn twice and publish only the
    winning attempt's lines."""
    published = _publish_capture(monkeypatch)
    monkeypatch.setattr(execute_mod.time, "sleep", lambda _s: None)

    attempts = iter([
        _FakeContainer([ROLLOUT_ERROR], 1),
        _FakeContainer([ROLLOUT_ERROR], 1),
        _FakeContainer(['{"type":"turn.completed"}'], 0),
    ])
    first_container = next(attempts)

    class _FakeContainers:
        def run(self, _image, **_kwargs):
            return next(attempts)

    class _FakeClient:
        containers = _FakeContainers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _FakeClient(), "img", {}, first_container, "run-x", _FakeRedis(), True)

    assert returncode == 0
    assert published == ['{"type":"turn.completed"}']


def test_retry_loop_gives_up_after_max_attempts(monkeypatch):
    """Contention on every attempt must not retry forever — bounded by
    _CODEX_ROLLOUT_MAX_ATTEMPTS, and the last attempt's error is visible."""
    published = _publish_capture(monkeypatch)
    monkeypatch.setattr(execute_mod.time, "sleep", lambda _s: None)
    spawned = {"count": 1}  # the caller already spawned attempt 1

    class _FakeContainers:
        def run(self, _image, **_kwargs):
            spawned["count"] += 1
            return _FakeContainer([ROLLOUT_ERROR], 1)

    class _FakeClient:
        containers = _FakeContainers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _FakeClient(), "img", {}, _FakeContainer([ROLLOUT_ERROR], 1),
        "run-x", _FakeRedis(), True)

    assert returncode == 1
    assert spawned["count"] == execute_mod._CODEX_ROLLOUT_MAX_ATTEMPTS
    assert published == [ROLLOUT_ERROR]


def test_non_codex_job_gets_exactly_one_attempt(monkeypatch):
    """codex_retryable=False must never respawn, even on a matching line —
    byte-for-byte the pre-retry behavior for claude and every other CLI."""
    published = _publish_capture(monkeypatch)

    class _FakeContainers:
        def run(self, _image, **_kwargs):
            raise AssertionError("must not respawn for a non-codex job")

    class _FakeClient:
        containers = _FakeContainers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _FakeClient(), "img", {}, _FakeContainer([ROLLOUT_ERROR], 1),
        "run-x", _FakeRedis(), False)

    assert returncode == 1
    assert published == [ROLLOUT_ERROR]


def test_run_job_blocking_still_publishes_done_when_the_retry_helper_raises(monkeypatch):
    """Card 3d15bf3b, blocking regression: the retry refactor moved
    _run_job_blocking's cleanup (kill_timer.cancel + _publish_done +
    execution_index.start) out from under any try/finally, so an exception
    escaping _run_cold_agent_with_retry (a dropped Redis connection mid-flush,
    a docker error, anything) left the run with no `done` event ever
    published — hangs forever from agents-platform's side — and, on a
    raw_command job, an armed kill timer that fires later.

    Reproduced here through a REAL escape path already in the retry loop:
    `time.sleep(backoff)` between a contention respawn and the next attempt
    is not itself guarded by any try/except, so a genuine hiccup there (or
    any other exception _run_cold_agent_with_retry doesn't already catch)
    propagates straight out of it. _run_job_blocking must still publish
    `done` and run its other cleanup regardless."""
    import sys
    import types

    fake_docker = types.ModuleType("docker")

    class _FakeImages:
        def pull(self, _image):
            pass

    class _FirstAttemptContainers:
        """The FIRST spawn (inside _run_job_blocking, before the retry loop
        even starts) reports the contention signature, forcing a retry —
        attempt 2 hits the unguarded time.sleep below before it can spawn a
        second container, so that spawn must never be reached."""

        def run(self, _image, **_kwargs):
            return _FakeContainer([ROLLOUT_ERROR], 1)

    fake_docker.DockerClient = lambda base_url=None: types.SimpleNamespace(
        images=_FakeImages(), containers=_FirstAttemptContainers())
    fake_docker.errors = types.SimpleNamespace(APIError=Exception)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/fake.sock")
    monkeypatch.setattr(execute_mod.warm_pool, "enabled", lambda: False)
    monkeypatch.setattr(execute_mod, "_build_container_kwargs",
                        lambda job: ("img", ["argv"], {}, None))
    monkeypatch.setattr(execute_mod, "_redis_client", lambda url: _FakeRedis())

    def _boom_sleep(_seconds):
        raise RuntimeError("redis dropped mid-retry")

    monkeypatch.setattr(execute_mod.time, "sleep", _boom_sleep)

    done_calls = []
    monkeypatch.setattr(execute_mod, "_publish_done",
                        lambda _r, run_id, rc: done_calls.append((run_id, rc)))
    index_started = []
    monkeypatch.setattr(execute_mod.execution_index, "start",
                        lambda run_id: index_started.append(run_id))

    job = {"run_id": "run-cleanup", "cli": "codex", "prompt": "hi", "permissions": {}}
    # Must not raise out to the caller — this runs in a daemon thread in
    # production with nothing to catch an escaped exception, which would
    # silently kill the thread and never publish `done`.
    execute_mod._run_job_blocking(job, "redis://example.test:6379/0")

    assert done_calls == [("run-cleanup", 1)]
    assert index_started == ["run-cleanup"]
