"""aw-warm-relay-codex.py retries a resume that fails with codex's own
"no rollout found for thread id ... (code -32600)" before any real turn
content was produced.

Card 3d15bf3b-9510-8164-95c8-d1c26da0df00: a debugger run found the rollout
.jsonl and its state_5.sqlite `threads` row both fully intact for a thread
that still failed to resume with this exact error — the leading explanation
is SQLITE_BUSY-class contention on the ONE $CODEX_HOME shared by every
concurrent codex process this runner dispatches (confirmed against codex's
own upstream source: state_5.sqlite gets a hardcoded, non-configurable 5s
busy_timeout with no retry beyond it — codex-rs/state/src/sqlite.rs — the
same contention class as open bugs openai/codex#20213 and #35555). Retrying
the whole subprocess is safe because this failure happens before codex ever
processes the prompt, and the tests here pin that the retry is INVISIBLE
(no error line reaches the run's Redis stream) unless every attempt fails.

Card 4055bf3b-9510-8179 (2026-10-26): live-reproduced against the deployed
cold-path fix — a `knowledgeable-playground` synthesize turn (which runs
through THIS warm relay, not the cold path) still died with zero tokens
after only emitting the codex CLI startup banner ("Reading additional input
from stdin..."), because this file duplicates the same signature-only check
and was not fixed alongside execute.py. _run_codex_turn's retry trigger is
now "attempt exited nonzero without any real output", not only a rollout
signature match.

Card 4055bf3b-9510-8179 follow-up (2026-10-26, see
docs/design/codex-runner-429-rate-limit-handling.md): a SECOND, independent
retry class for codex's own 429-exhaustion event (_is_rate_limit_exhaustion),
mirroring execute.py's own _is_codex_rate_limit_exhaustion, on its own
long/jittered schedule, coordinated through the SAME shared Redis cooldown
key execute.py's cold path arms/waits on.

Run: .venv/aw/bin/python -m pytest tests/test_warm_relay_codex_retry.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY_PATH = ROOT / "agent-images" / "shared" / "aw-warm-relay-codex.py"

ROLLOUT_ERROR = ("Error: thread/resume: thread/resume failed: no rollout "
                  "found for thread id 01a06cc7-c10e-7123-b9ac-ba1335398bcd "
                  "(code -32600)")

RATE_LIMIT_ERROR = ('{"type":"error","message":"exceeded retry limit, last '
                     'status: 429 Too Many Requests"}')


def _load_relay():
    """Import the relay by path (its filename has dashes) — same technique
    as test_warm_relay_done_sentinel.py's _load_relay."""
    if "redis" not in sys.modules:
        stub = type(sys)("redis")
        stub.from_url = lambda *_a, **_kw: None
        sys.modules["redis"] = stub
    spec = importlib.util.spec_from_file_location("aw_warm_relay_codex", str(RELAY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRedis:
    """Stream-publish double (self.lines) plus a minimal in-memory
    GET/SET-with-ex store for the shared 429 cooldown key — added for card
    4055bf3b-9510-8179's follow-up; `ex` is accepted and ignored, no test
    here depends on real expiry."""

    def __init__(self):
        self.lines: list[str] = []
        self._kv: dict[str, str] = {}

    def xadd(self, _key, fields, **_kw):
        if "line" in fields:
            self.lines.append(fields["line"])

    def get(self, key):
        return self._kv.get(key)

    def set(self, key, value, ex=None):
        self._kv[key] = str(value)
        return True

    def delete(self, key):
        self._kv.pop(key, None)


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = [l + "\n" for l in lines]

    def __iter__(self):
        return iter(self._lines)


class _FakeProc:
    def __init__(self, lines: list[str], rc: int):
        self.stdout = _FakeStdout(lines)
        self._rc = rc

    def wait(self):
        return self._rc


def _popen_sequence(monkeypatch, relay, attempts: list[tuple[list[str], int]]):
    """Each call to subprocess.Popen returns the next canned (lines, rc)."""
    calls = iter(attempts)

    def _fake_popen(*_a, **_kw):
        lines, rc = next(calls)
        return _FakeProc(lines, rc)

    monkeypatch.setattr(relay.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(relay, "aw_attach", None)


def test_ordinary_turn_streams_live_no_retry(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        (['{"type":"thread.started"}', '{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 1
    assert r.lines == ['{"type":"thread.started"}', '{"type":"turn.completed"}']


def test_contention_then_success_hides_the_failed_attempts(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([ROLLOUT_ERROR], 1),
        ([ROLLOUT_ERROR], 1),
        (['{"type":"thread.started"}', '{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 3
    # Only the successful attempt's lines reached the run's event stream —
    # a retry must be invisible to anything consuming it.
    assert r.lines == ['{"type":"thread.started"}', '{"type":"turn.completed"}']
    assert not any(ROLLOUT_ERROR in l for l in r.lines)


def test_retries_are_bounded_and_the_last_error_is_still_visible(monkeypatch):
    """Retries must not paper over a genuinely-broken thread forever — once
    exhausted, the error is published, not silently swallowed."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([ROLLOUT_ERROR], 1),
        ([ROLLOUT_ERROR], 1),
        ([ROLLOUT_ERROR], 1),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 1
    assert attempts == relay._MAX_RESUME_ATTEMPTS
    assert r.lines == [ROLLOUT_ERROR]


BANNER_LINE = "Reading additional input from stdin..."


def test_banner_only_death_is_retried(monkeypatch):
    """The actual production failure (card 4055bf3b-9510-8179): codex prints
    only its own startup banner then dies, under concurrent warm-turn load —
    no rollout signature anywhere, but still retryable."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE], 1),
        (['{"type":"thread.started"}', '{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 2
    assert r.lines == ['{"type":"thread.started"}', '{"type":"turn.completed"}']
    assert not any(BANNER_LINE in l for l in r.lines)


def test_banner_only_death_exhausts_retries_and_still_publishes(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE], 1),
        ([BANNER_LINE], 1),
        ([BANNER_LINE], 1),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 1
    assert attempts == relay._MAX_RESUME_ATTEMPTS
    assert r.lines == [BANNER_LINE]


def test_silent_attempt_with_no_output_at_all_is_retried(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([], 1),
        (['{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 2
    assert r.lines == ['{"type":"turn.completed"}']


def test_a_different_failure_is_not_retried(monkeypatch):
    """A failure that prints real (non-boilerplate) content must surface on
    the first attempt, same as before this change existed — only an attempt
    producing NO real output at all is retryable."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        (["error: unexpected argument '--foo' found"], 2),
        (["this attempt must never run"], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 2
    assert attempts == 1
    assert r.lines == ["error: unexpected argument '--foo' found"]


def test_signature_arriving_after_real_content_is_not_retried(monkeypatch):
    """The signature only means contention when it is the FIRST thing codex
    prints. Once real content has streamed, a later line merely mentioning
    the same words (e.g. quoted in assistant text) must not trigger a
    doomed-attempt retry — the turn already succeeded."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        (['{"type":"turn.completed"}', ROLLOUT_ERROR], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 1
    assert r.lines == ['{"type":"turn.completed"}', ROLLOUT_ERROR]


def test_signature_with_zero_returncode_is_not_treated_as_contention(monkeypatch):
    """Belt-and-braces: the rollout-error text alone isn't enough to retry —
    codex would have to both print it AND exit non-zero, since that text
    could plausibly appear as genuine quoted model output. A line that
    happens to contain the phrase but a clean exit must not spin up
    retries (unlike _THREAD_STARTED_SIGNATURE/_PREAMBLE_SIGNATURES below,
    which retry regardless of exit code)."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([ROLLOUT_ERROR], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 1
    assert r.lines == [ROLLOUT_ERROR]


def test_thread_started_only_death_is_retried_even_with_clean_exit(monkeypatch):
    """Card 4055bf3b-9510-8179 follow-up (2026-10-26): the actual production
    failure against search-knowledge's synthesize node under a large (~58KB)
    context — codex starts a session (thread.started) then dies CLEANLY
    (exit 0), no real turn content ever following. Confirmed live via
    run_events: node_start -> cli.first_byte -> thread.started ->
    finalizing(tokens=0) -> node_end, ~3.4s, no retry/backoff pattern,
    exactly because the old code treated thread.started as "real" output."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE, '{"type":"thread.started"}'], 0),
        (['{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 2
    assert r.lines == ['{"type":"turn.completed"}']
    assert not any("thread.started" in l for l in r.lines)


def test_thread_started_only_death_exhausts_retries_and_still_publishes(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE, '{"type":"thread.started"}'], 0),
        ([BANNER_LINE, '{"type":"thread.started"}'], 0),
        ([BANNER_LINE, '{"type":"thread.started"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == relay._MAX_RESUME_ATTEMPTS
    assert r.lines == [BANNER_LINE, '{"type":"thread.started"}']


def test_turn_started_only_death_is_retried_even_with_clean_exit(monkeypatch):
    """Round 2 of the same card (2026-10-26): the thread-started fix alone
    deployed clean but the production symptom persisted — live DIAG2
    instrumentation caught `{"type":"turn.started"}` as the actual line
    flipping `live` on every single codex invocation observed, success or
    failure alike (codex's own handshake is thread.started -> turn.started
    -> item.*). A process that dies right after turn.started is exactly as
    uninformative as one that dies right after thread.started — confirmed
    live against 3 separate knowledgeable-search-planner failures at
    08:26:11-17 UTC, same batch as a synthesize call that happened to
    succeed."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0),
        (['{"type":"turn.completed"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 2
    assert r.lines == ['{"type":"turn.completed"}']
    assert not any("started" in l for l in r.lines)


def test_turn_started_only_death_exhausts_retries_and_still_publishes(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0),
        ([BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0),
        ([BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}'], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == relay._MAX_RESUME_ATTEMPTS
    assert r.lines == [
        BANNER_LINE, '{"type":"thread.started"}', '{"type":"turn.started"}']


# ── Card 4055bf3b-9510-8179 follow-up: the 429 rate-limit retry class + the
# shared Redis cooldown gate — docs/design/codex-runner-429-rate-limit-handling.md


def test_rate_limit_signature_matches_structural_shape():
    relay = _load_relay()
    assert relay._is_rate_limit_exhaustion(RATE_LIMIT_ERROR) is True


def test_rate_limit_signature_requires_type_error_not_a_substring_match():
    relay = _load_relay()
    quoted = ('{"type":"assistant","message":{"content":[{"type":"text",'
              '"text":"exceeded retry limit, last status: 429 Too Many Requests"}]}}')
    assert relay._is_rate_limit_exhaustion(quoted) is False


def test_rate_limit_signature_requires_both_fragments():
    relay = _load_relay()
    assert relay._is_rate_limit_exhaustion(
        '{"type":"error","message":"exceeded retry limit"}') is False
    assert relay._is_rate_limit_exhaustion(
        '{"type":"error","message":"429 Too Many Requests"}') is False


def test_rate_limit_exhaustion_is_retried_even_with_a_clean_exit(monkeypatch):
    """Never gated on returncode, unlike _ROLLOUT_ERROR_SIGNATURE — codex can
    die cleanly here too."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([RATE_LIMIT_ERROR], 0),
        (['{"type":"turn.completed"}'], 0),
    ])
    monkeypatch.setattr(relay.time, "sleep", lambda _s: None)
    monkeypatch.setattr(relay, "_cooldown_wait", lambda *_a, **_kw: None)
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 2
    assert r.lines[-1] == '{"type":"turn.completed"}'
    assert not any("exceeded retry limit" in l for l in r.lines)


def test_rate_limit_exhaustion_exhausts_retries_and_still_publishes(monkeypatch):
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([RATE_LIMIT_ERROR], 1),
        ([RATE_LIMIT_ERROR], 1),
        ([RATE_LIMIT_ERROR], 1),
    ])
    monkeypatch.setattr(relay.time, "sleep", lambda _s: None)
    monkeypatch.setattr(relay, "_cooldown_wait", lambda *_a, **_kw: None)
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 1
    assert attempts == relay._RATE_LIMIT_MAX_ATTEMPTS
    assert r.lines[-1] == RATE_LIMIT_ERROR
    assert len(r.lines) == relay._RATE_LIMIT_MAX_ATTEMPTS - 1 + 1
    assert all(json.loads(l)["type"] == "runner.notice" for l in r.lines[:-1])


def test_rate_limit_retry_uses_its_own_schedule_and_arms_the_shared_cooldown(monkeypatch):
    """The rate-limit backoff (20/60s) must never be confused with
    contention's 0.3/0.6s one, and the shared cooldown key must be armed on
    every retry — the SAME key execute.py's cold path reads."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([RATE_LIMIT_ERROR], 1),
        (['{"type":"turn.completed"}'], 0),
    ])
    sleeps = []
    monkeypatch.setattr(relay.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(relay.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(relay, "_cooldown_wait", lambda *_a, **_kw: None)
    r = _FakeRedis()

    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")

    assert rc == 0
    assert attempts == 2
    assert sleeps == [20.0]  # the rate-limit schedule, never 0.3/0.6
    assert r.get(relay._COOLDOWN_KEY) == "30"
    notice = json.loads(r.lines[0])
    assert notice["type"] == "runner.notice"
    assert r.lines[1:] == ['{"type":"turn.completed"}']


def test_cooldown_arm_doubles_while_active_and_caps():
    relay = _load_relay()
    r = _FakeRedis()
    relay._cooldown_arm(r)
    assert r.get(relay._COOLDOWN_KEY) == "30"
    relay._cooldown_arm(r)
    assert r.get(relay._COOLDOWN_KEY) == "60"
    relay._cooldown_arm(r)
    assert r.get(relay._COOLDOWN_KEY) == "120"
    relay._cooldown_arm(r)
    assert r.get(relay._COOLDOWN_KEY) == "120"  # capped


def test_cooldown_wait_is_a_noop_when_nothing_is_armed(monkeypatch):
    relay = _load_relay()
    r = _FakeRedis()
    sleeps = []
    monkeypatch.setattr(relay.time, "sleep", lambda s: sleeps.append(s))
    relay._cooldown_wait(r, "run:x:events", "x")
    assert sleeps == []
    assert r.lines == []


def test_cooldown_wait_polls_until_cleared_then_adds_release_jitter(monkeypatch):
    relay = _load_relay()
    r = _FakeRedis()
    r.set(relay._COOLDOWN_KEY, "30", ex=30)
    sleeps = []
    calls = {"n": 0}

    def _fake_sleep(s):
        sleeps.append(s)
        calls["n"] += 1
        if calls["n"] == 2:
            r.delete(relay._COOLDOWN_KEY)

    monkeypatch.setattr(relay.time, "sleep", _fake_sleep)
    monkeypatch.setattr(relay.random, "uniform", lambda _a, _b: 2.5)

    relay._cooldown_wait(r, "run:x:events", "x")

    assert sleeps == [1, 1, 2.5]
    assert len(r.lines) == 1
    assert json.loads(r.lines[0])["type"] == "runner.notice"


def test_cooldown_wait_fails_open_after_the_bound_with_no_release_jitter(monkeypatch):
    relay = _load_relay()
    r = _FakeRedis()
    r.set(relay._COOLDOWN_KEY, "120", ex=120)
    monkeypatch.setattr(relay, "_COOLDOWN_WAIT_BOUND_S", 3)
    sleeps = []
    monkeypatch.setattr(relay.time, "sleep", lambda s: sleeps.append(s))
    jitter_calls = []
    monkeypatch.setattr(relay.random, "uniform",
                        lambda a, b: jitter_calls.append((a, b)) or 0.0)

    relay._cooldown_wait(r, "run:x:events", "x")

    assert sleeps == [1, 1, 1]
    assert jitter_calls == []  # never cleared, so no release jitter
