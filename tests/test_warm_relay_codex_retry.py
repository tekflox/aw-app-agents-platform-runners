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

Run: .venv/aw/bin/python -m pytest tests/test_warm_relay_codex_retry.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY_PATH = ROOT / "agent-images" / "shared" / "aw-warm-relay-codex.py"

ROLLOUT_ERROR = ("Error: thread/resume: thread/resume failed: no rollout "
                  "found for thread id 01a06cc7-c10e-7123-b9ac-ba1335398bcd "
                  "(code -32600)")


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
    def __init__(self):
        self.lines: list[str] = []

    def xadd(self, _key, fields, **_kw):
        if "line" in fields:
            self.lines.append(fields["line"])


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


def test_a_different_failure_is_not_retried(monkeypatch):
    """Only the specific rollout-contention signature is retryable — any
    other failure must surface on the first attempt, same as before this
    change existed."""
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
        (['{"type":"thread.started"}', ROLLOUT_ERROR], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 1
    assert r.lines == ['{"type":"thread.started"}', ROLLOUT_ERROR]


def test_signature_with_zero_returncode_is_not_treated_as_contention(monkeypatch):
    """Belt-and-braces: the signature alone isn't enough to retry — codex
    would have to both print it AND exit non-zero. A line that happens to
    contain the phrase but a clean exit must not spin up retries."""
    relay = _load_relay()
    _popen_sequence(monkeypatch, relay, [
        ([ROLLOUT_ERROR], 0),
    ])
    r = _FakeRedis()
    rc, attempts, tail, _auth_revoked = relay._run_codex_turn(["codex"], "/", {}, r, "run:x:events", "x")
    assert rc == 0
    assert attempts == 1
    assert r.lines == [ROLLOUT_ERROR]
