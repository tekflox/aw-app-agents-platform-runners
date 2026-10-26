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

Card 4055bf3b-9510-8179 (2026-10-26) widened the trigger: under heavier
concurrent codex-spawn load (confirmed across two unrelated consumers,
search-knowledge and literature-review, hitting bursts of 5 simultaneous
spawns failing 100%) the process sometimes dies having emitted only its own
CLI startup banner ("Reading additional input from stdin...") — no rollout
signature at all — which the original signature-only check never retried.
contention_hit is now True whenever a codex attempt exits nonzero without
ever producing a line of real output, not only when every line matched the
rollout signature.

Card 4055bf3b-9510-8179 follow-up (2026-10-26, see
docs/design/codex-runner-429-rate-limit-handling.md): a SECOND, independent
retry class for codex's own 429-exhaustion event
(_is_codex_rate_limit_exhaustion), on its own long/jittered schedule,
coordinated through the shared Redis cooldown key
(_codex_429_cooldown_arm/_codex_429_cooldown_wait). _stream_cold_attempt's
return value grew from a bare contention_hit bool to a
"contention" | "rate_limit" | None reason string.

Run: .venv/aw/bin/python -m pytest tests/test_execute_codex_rollout_retry.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

ROLLOUT_ERROR = ("Error: thread/resume: thread/resume failed: no rollout "
                  "found for thread id 01a06cc7-c10e-7123-b9ac-ba1335398bcd "
                  "(code -32600)")

RATE_LIMIT_ERROR = ('{"type":"error","message":"exceeded retry limit, last '
                     'status: 429 Too Many Requests"}')


class _FakeRedis:
    """Bare pub/sub double by default (self.lines) plus a minimal in-memory
    GET/SET-with-ex store for the shared 429 cooldown key — added for card
    4055bf3b-9510-8179's follow-up (the cooldown gate itself; `ex` is
    accepted and ignored, no test here depends on real expiry)."""

    def __init__(self):
        self.lines: list[str] = []
        self._kv: dict[str, str] = {}

    def get(self, key):
        return self._kv.get(key)

    def set(self, key, value, ex=None):
        self._kv[key] = str(value)
        return True

    def delete(self, key):
        self._kv.pop(key, None)


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
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", True)
    assert rc == 0
    assert reason is None
    assert published == ['{"type":"thread.started"}', '{"type":"turn.completed"}']


def test_contention_hit_publishes_nothing_when_a_retry_follows(monkeypatch):
    """A doomed attempt the caller is about to retry must be invisible —
    nothing reaches the run's event stream, so a retry looks clean."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 1
    assert reason == "contention"
    assert published == []


def test_contention_hit_on_the_last_attempt_still_publishes(monkeypatch):
    """Once the caller has decided not to retry again, a withheld contention
    error must still surface — never silently swallowed (this is what
    test_retry_loop_gives_up_after_max_attempts exercises end to end via
    _run_cold_agent_with_retry; this pins the single-attempt contract it
    depends on)."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 1
    assert reason == "contention"
    assert published == [ROLLOUT_ERROR]


def test_non_codex_job_never_treats_it_as_retryable(monkeypatch):
    """codex_retryable=False (any non-codex CLI) must behave exactly like
    the loop did before retry support existed — publish immediately, no
    contention detection at all."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", False)
    assert rc == 1
    assert reason is None
    assert published == [ROLLOUT_ERROR]


BANNER_LINE = "Reading additional input from stdin..."


def test_banner_only_death_is_retryable(monkeypatch):
    """The actual production failure signature (card 4055bf3b-9510-8179):
    codex prints only its own startup banner, then dies — no rollout
    signature anywhere — under concurrent spawn load."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 1
    assert reason == "contention"
    assert published == []


def test_banner_only_death_on_last_attempt_still_publishes(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 1
    assert reason == "contention"
    assert published == [BANNER_LINE]


def test_silent_death_with_no_lines_at_all_is_retryable(monkeypatch):
    """Even a process that never emits a single line (not even the banner)
    before dying must be retryable — the common thread is "no real output",
    not any specific string."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 1
    assert reason == "contention"
    assert published == []


def test_non_codex_banner_only_death_is_not_retryable(monkeypatch):
    """codex_retryable=False must never treat a banner-only death as
    contention — byte-for-byte the pre-retry behavior for every non-codex
    CLI, same contract as test_non_codex_job_never_treats_it_as_retryable."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE], 1)
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", False)
    assert rc == 1
    assert reason is None
    assert published == [BANNER_LINE]


def test_banner_then_real_content_is_not_contention(monkeypatch):
    """The banner is buffered, not dropped — once real output follows, the
    banner line must still reach the stream, in order, same flush contract
    as a rollout-signature line followed by real content."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE, '{"type":"turn.completed"}'], 0)
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", True)
    assert rc == 0
    assert reason is None
    assert published == [BANNER_LINE, '{"type":"turn.completed"}']


def test_signature_after_real_content_is_not_contention(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer(['{"type":"turn.completed"}', ROLLOUT_ERROR], 0)
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", True)
    assert rc == 0
    assert reason is None
    assert published == ['{"type":"turn.completed"}', ROLLOUT_ERROR]


def test_thread_started_only_death_is_retryable(monkeypatch):
    """Card 4055bf3b-9510-8179 follow-up (2026-10-26): the actual production
    failure against search-knowledge's synthesize node under a large (~58KB)
    context — codex starts a session (thread.started) then dies CLEANLY
    (returncode 0), no real turn content ever following. Confirmed live via
    run_events: node_start -> cli.first_byte -> thread.started ->
    finalizing(tokens=0) -> node_end, ~3.4s, no retry/backoff pattern,
    exactly because the old code treated thread.started as "real" output
    regardless of exit code."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE, '{"type":"thread.started"}'], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 0
    assert reason == "contention"
    assert published == []


def test_thread_started_only_death_on_last_attempt_still_publishes(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([BANNER_LINE, '{"type":"thread.started"}'], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 0
    assert reason == "contention"
    assert published == [BANNER_LINE, '{"type":"thread.started"}']


def test_turn_started_only_death_is_retryable(monkeypatch):
    """Round 2 of the same card (2026-10-26): the thread-started fix alone
    deployed clean but the production symptom persisted — live DIAG2
    instrumentation caught `{"type":"turn.started"}` as the actual line
    flipping `live` on every single codex invocation observed, success or
    failure alike. codex's own session handshake is thread.started ->
    turn.started -> item.*, so a process that dies right after turn.started
    (confirmed live against 3 separate knowledgeable-search-planner failures
    at 08:26:11-17 UTC, same run batch) is exactly as uninformative as one
    that dies right after thread.started."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer(
        [BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 0
    assert reason == "contention"
    assert published == []


def test_turn_started_only_death_on_last_attempt_still_publishes(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer(
        [BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 0
    assert reason == "contention"
    assert published == [
        BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}']


def test_rollout_signature_alone_with_clean_exit_is_still_not_contention(monkeypatch):
    """Belt-and-braces (mirrors aw-warm-relay-codex.py's own test): unlike
    the thread-started/preamble widening above, the rollout-error text alone
    must still require a nonzero returncode to count as contention — that
    text could plausibly appear as genuine quoted model output."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([ROLLOUT_ERROR], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 0
    assert reason is None
    assert published == [ROLLOUT_ERROR]


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
                        lambda job, *_a, **_k: ("img", ["argv"], {}, None))
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


# ── Card 4055bf3b-9510-8179 follow-up: the 429 rate-limit retry class + the
# shared Redis cooldown gate — docs/design/codex-runner-429-rate-limit-handling.md


def test_rate_limit_signature_matches_structural_shape():
    assert execute_mod._is_codex_rate_limit_exhaustion(RATE_LIMIT_ERROR) is True


def test_rate_limit_signature_requires_type_error_not_a_substring_match():
    """The same text quoted inside genuine model output (not a real
    top-level codex error event) must not match — belt-and-braces for the
    exact risk the design doc flags: a substring match could misclassify
    real content."""
    quoted = ('{"type":"assistant","message":{"content":[{"type":"text",'
              '"text":"exceeded retry limit, last status: 429 Too Many Requests"}]}}')
    assert execute_mod._is_codex_rate_limit_exhaustion(quoted) is False


def test_rate_limit_signature_requires_both_fragments():
    assert execute_mod._is_codex_rate_limit_exhaustion(
        '{"type":"error","message":"exceeded retry limit"}') is False
    assert execute_mod._is_codex_rate_limit_exhaustion(
        '{"type":"error","message":"429 Too Many Requests"}') is False


def test_rate_limit_signature_rejects_non_json():
    assert execute_mod._is_codex_rate_limit_exhaustion("not json at all") is False


def test_rate_limit_exhaustion_is_retryable_with_a_clean_exit(monkeypatch):
    """Never gated on returncode — risk 1 in the design doc: codex can die
    cleanly here just as it can for the session-handshake signatures."""
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([RATE_LIMIT_ERROR], 0)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert rc == 0
    assert reason == "rate_limit"
    assert published == []


def test_rate_limit_exhaustion_on_last_attempt_still_publishes(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([RATE_LIMIT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=True)
    assert rc == 1
    assert reason == "rate_limit"
    assert published == [RATE_LIMIT_ERROR]


def test_rate_limit_takes_priority_over_rollout_contention_classification(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([RATE_LIMIT_ERROR, ROLLOUT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(
        container, _FakeRedis(), "run-x", True, is_last_attempt=False)
    assert reason == "rate_limit"
    assert published == []


def test_non_codex_job_never_treats_rate_limit_as_retryable(monkeypatch):
    published = _publish_capture(monkeypatch)
    container = _FakeContainer([RATE_LIMIT_ERROR], 1)
    rc, reason = execute_mod._stream_cold_attempt(container, _FakeRedis(), "run-x", False)
    assert rc == 1
    assert reason is None
    assert published == [RATE_LIMIT_ERROR]


def _fake_clock(monkeypatch):
    """Patch both time.monotonic and time.sleep to a controllable fake
    clock — _sleep_abortable computes its deadline off time.monotonic(), so
    patching only time.sleep (as the rest of this file's tests do for the
    short, unsliced contention backoff) would busy-loop for the full real
    duration of a tens-of-seconds rate-limit backoff."""
    state = {"t": 0.0, "sleeps": []}
    monkeypatch.setattr(execute_mod.time, "monotonic", lambda: state["t"])

    def _sleep(s):
        state["sleeps"].append(s)
        state["t"] += s

    monkeypatch.setattr(execute_mod.time, "sleep", _sleep)
    return state


def test_sleep_abortable_completes_without_abort(monkeypatch):
    clock = _fake_clock(monkeypatch)
    aborted = execute_mod._sleep_abortable(_FakeRedis(), "run-x", 2.5, slice_s=1.0)
    assert aborted is False
    assert clock["sleeps"] == [1.0, 1.0, 0.5]


def test_sleep_abortable_returns_true_when_aborted_mid_sleep(monkeypatch):
    clock = _fake_clock(monkeypatch)
    r = _FakeRedis()
    execute_mod.mark_aborted(r, "run-sleep-abort")
    try:
        aborted = execute_mod._sleep_abortable(r, "run-sleep-abort", 10.0, slice_s=1.0)
        assert aborted is True
        assert clock["sleeps"] == []  # caught on the first check, before any sleep
    finally:
        execute_mod.clear_abort(r, "run-sleep-abort")


def test_cooldown_arm_doubles_while_active_and_caps(monkeypatch):
    r = _FakeRedis()
    execute_mod._codex_429_cooldown_arm(r)
    assert r.get(execute_mod._CODEX_429_COOLDOWN_KEY) == "30"
    execute_mod._codex_429_cooldown_arm(r)
    assert r.get(execute_mod._CODEX_429_COOLDOWN_KEY) == "60"
    execute_mod._codex_429_cooldown_arm(r)
    assert r.get(execute_mod._CODEX_429_COOLDOWN_KEY) == "120"
    execute_mod._codex_429_cooldown_arm(r)
    assert r.get(execute_mod._CODEX_429_COOLDOWN_KEY) == "120"  # capped


def test_cooldown_wait_is_a_noop_when_nothing_is_armed(monkeypatch):
    r = _FakeRedis()
    sleeps = []
    monkeypatch.setattr(execute_mod.time, "sleep", lambda s: sleeps.append(s))
    published = _publish_capture(monkeypatch)
    proceed = execute_mod._codex_429_cooldown_wait(r, "run-x")
    assert proceed is True
    assert sleeps == []
    assert published == []


def test_cooldown_wait_polls_until_cleared_then_adds_release_jitter(monkeypatch):
    r = _FakeRedis()
    r.set(execute_mod._CODEX_429_COOLDOWN_KEY, "30", ex=30)
    sleeps = []
    calls = {"n": 0}

    def _fake_sleep(s):
        sleeps.append(s)
        calls["n"] += 1
        if calls["n"] == 2:  # the cooldown clears on its own after 2 polls
            r.delete(execute_mod._CODEX_429_COOLDOWN_KEY)

    monkeypatch.setattr(execute_mod.time, "sleep", _fake_sleep)
    monkeypatch.setattr(execute_mod.random, "uniform", lambda _a, _b: 2.5)
    published = _publish_capture(monkeypatch)

    proceed = execute_mod._codex_429_cooldown_wait(r, "run-x")

    assert proceed is True
    assert sleeps == [1, 1, 2.5]  # two 1s polls, then the release jitter
    assert len(published) == 1
    notice = json.loads(published[0])
    assert notice["type"] == "runner.notice"


def test_cooldown_wait_fails_open_after_the_bound_with_no_release_jitter(monkeypatch):
    r = _FakeRedis()
    r.set(execute_mod._CODEX_429_COOLDOWN_KEY, "120", ex=120)
    monkeypatch.setattr(execute_mod, "_CODEX_429_COOLDOWN_WAIT_BOUND_S", 3)
    sleeps = []
    monkeypatch.setattr(execute_mod.time, "sleep", lambda s: sleeps.append(s))
    jitter_calls = []
    monkeypatch.setattr(execute_mod.random, "uniform",
                        lambda a, b: jitter_calls.append((a, b)) or 0.0)
    _publish_capture(monkeypatch)

    proceed = execute_mod._codex_429_cooldown_wait(r, "run-x")

    assert proceed is True  # fail-open, never freezes the fleet
    assert sleeps == [1, 1, 1]
    assert jitter_calls == []  # it never actually cleared, so no release jitter


def test_cooldown_wait_returns_false_when_aborted_mid_wait(monkeypatch):
    r = _FakeRedis()
    r.set(execute_mod._CODEX_429_COOLDOWN_KEY, "30", ex=30)
    execute_mod.mark_aborted(r, "run-abort-cooldown")
    try:
        monkeypatch.setattr(execute_mod.time, "sleep", lambda _s: None)
        _publish_capture(monkeypatch)
        proceed = execute_mod._codex_429_cooldown_wait(r, "run-abort-cooldown")
        assert proceed is False
    finally:
        execute_mod.clear_abort(r, "run-abort-cooldown")


def test_retry_loop_respawns_on_rate_limit_with_its_own_schedule_and_arms_cooldown(monkeypatch):
    """End-to-end over the real _run_cold_agent_with_retry: a rate_limit hit
    must use the long schedule (not contention's 0.3s/0.6s), arm the shared
    cooldown, publish a notice, and still hide the failed attempt's own
    lines exactly like a contention retry does."""
    published = _publish_capture(monkeypatch)
    clock = _fake_clock(monkeypatch)
    monkeypatch.setattr(execute_mod.random, "uniform", lambda _a, _b: 0.0)
    # The cooldown-wait gate is exercised by its own dedicated tests above;
    # isolating it here keeps this test about the retry loop's own backoff
    # and arm behavior, not about re-proving the wait mechanism works.
    monkeypatch.setattr(execute_mod, "_codex_429_cooldown_wait", lambda _r, _run_id: True)

    r = _FakeRedis()
    attempts = iter([
        _FakeContainer([RATE_LIMIT_ERROR], 1),
        _FakeContainer(['{"type":"turn.completed"}'], 0),
    ])
    first_container = next(attempts)

    class _FakeContainers:
        def run(self, _image, **_kwargs):
            return next(attempts)

    class _FakeClient:
        containers = _FakeContainers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _FakeClient(), "img", {}, first_container, "run-x", r, True)

    assert returncode == 0
    assert len(published) == 2
    notice = json.loads(published[0])
    assert notice["type"] == "runner.notice"
    assert "20s" in notice["text"] and "attempt 2/3" in notice["text"]
    assert published[1] == '{"type":"turn.completed"}'
    # the rate-limit schedule (20s total, sliced into 1s abortable chunks),
    # never the 0.3/0.6s contention one
    assert sum(clock["sleeps"]) == 20.0
    assert r.get(execute_mod._CODEX_429_COOLDOWN_KEY) == "30"


def test_retry_loop_bounds_rate_limit_attempts(monkeypatch):
    published = _publish_capture(monkeypatch)
    _fake_clock(monkeypatch)
    monkeypatch.setattr(execute_mod.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(execute_mod, "_codex_429_cooldown_wait", lambda _r, _run_id: True)
    spawned = {"count": 1}

    class _FakeContainers:
        def run(self, _image, **_kwargs):
            spawned["count"] += 1
            return _FakeContainer([RATE_LIMIT_ERROR], 1)

    class _FakeClient:
        containers = _FakeContainers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _FakeClient(), "img", {}, _FakeContainer([RATE_LIMIT_ERROR], 1),
        "run-x", _FakeRedis(), True)

    assert returncode == 1
    assert spawned["count"] == execute_mod._CODEX_RATE_LIMIT_MAX_ATTEMPTS
    # a notice per retry, then the real error flushed once retries are exhausted
    assert published[-1] == RATE_LIMIT_ERROR
    assert len(published) == execute_mod._CODEX_RATE_LIMIT_MAX_ATTEMPTS - 1 + 1
    assert all(json.loads(p)["type"] == "runner.notice" for p in published[:-1])
