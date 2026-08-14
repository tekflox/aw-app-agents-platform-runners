"""The warm relay's "done" sentinel — one per DISPATCH, not one per turn.

claude re-invokes itself when work it backgrounded finishes, and each of
those continuations emits its own ``{"type":"result"}``. The relay used to
publish a done sentinel for every one of them, which is wrong in a way that
lands on somebody else: the container is warm and outlives the dispatch, so
a late continuation's lines carry whatever ``current_run_id`` holds when
they arrive — and agents-platform stops consuming a run at the first done it
sees. A background task finishing after the next turn was dispatched
therefore truncated an innocent, still-running run.

The dispatched turn is the only result with no ``origin``, which is what
these tests pin.

Run: .venv/aw/bin/python -m pytest tests/test_warm_relay_done_sentinel.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RELAY_PATH = ROOT / "agent-images" / "shared" / "aw-warm-relay.py"


def _load_relay():
    spec = importlib.util.spec_from_file_location("aw_warm_relay", str(RELAY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRedis:
    def __init__(self):
        self.adds: list[tuple[str, dict]] = []

    def xadd(self, key, fields, **_kw):
        self.adds.append((key, fields))

    def expire(self, *_a, **_kw):
        pass

    def dones(self) -> list[str]:
        """Stream keys that got a done sentinel, in order."""
        return [k for k, f in self.adds if "done" in f]


def _result(**extra) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, **extra})


def _run_relay(monkeypatch, tmp_path, lines, run_ids):
    """Feed `lines` through the relay. `run_ids` is read positionally — the
    Nth line is tagged with the Nth entry, mimicking dispatch_turn() rewriting
    current_run_id between turns."""
    relay = _load_relay()
    rundir = tmp_path / "rundir"
    rundir.mkdir()
    run_id_file = rundir / "current_run_id"
    run_id_file.write_text(run_ids[0])

    fake = _FakeRedis()
    monkeypatch.setattr(relay, "aw_attach", None)
    monkeypatch.setattr(relay.redis, "from_url", lambda *_a, **_kw: fake)
    monkeypatch.setattr(relay.sys, "argv", ["aw-warm-relay.py", str(rundir)])

    seq = iter(run_ids)

    class _Stdin(io.StringIO):
        def __next__(self):
            # advance current_run_id exactly as the host does between turns
            try:
                run_id_file.write_text(next(seq))
            except StopIteration:
                pass
            return super().__next__()

    monkeypatch.setattr(relay.sys, "stdin", _Stdin("".join(l + "\n" for l in lines)))
    assert relay.main() == 0
    return fake


def test_dispatched_turn_still_finalises(monkeypatch, tmp_path):
    """The ordinary case must be untouched — one turn, one done."""
    fake = _run_relay(monkeypatch, tmp_path, [_result()], ["run-a"])
    assert fake.dones() == ["run:run-a:events"]


def test_continuation_does_not_finalise(monkeypatch, tmp_path):
    """A task-notification continuation is not the end of a dispatch."""
    lines = [_result(), _result(origin={"kind": "task-notification"})]
    fake = _run_relay(monkeypatch, tmp_path, lines, ["run-a", "run-a"])
    assert fake.dones() == ["run:run-a:events"], "continuation published a second done"


def test_continuation_cannot_finalise_someone_elses_run(monkeypatch, tmp_path):
    """The bug that made this matter: run-a's background agent comes back
    after run-b was dispatched into the same warm container. The stale
    continuation must not terminate run-b."""
    lines = [_result(), _result(origin={"kind": "task-notification"})]
    fake = _run_relay(monkeypatch, tmp_path, lines, ["run-a", "run-b"])
    assert "run:run-b:events" not in fake.dones()
    assert fake.dones() == ["run:run-a:events"]


@pytest.mark.parametrize("kind", ["task-notification", "wakeup", "anything-else"])
def test_any_origin_kind_is_a_continuation(monkeypatch, tmp_path, kind):
    """`origin` is the discriminator, not the specific kind — a new harness
    trigger must not silently start finalising runs again."""
    fake = _run_relay(monkeypatch, tmp_path, [_result(origin={"kind": kind})], ["run-a"])
    assert fake.dones() == []


def test_done_is_idempotent_per_run(monkeypatch, tmp_path):
    """Two dispatched results for one run_id still yield one done."""
    fake = _run_relay(monkeypatch, tmp_path, [_result(), _result()], ["run-a", "run-a"])
    assert fake.dones() == ["run:run-a:events"]


def test_stdout_is_still_relayed_for_continuations(monkeypatch, tmp_path):
    """Withholding the sentinel must not withhold the output — the lines
    still go to the stream, they just don't close it."""
    lines = [json.dumps({"type": "assistant"}), _result(origin={"kind": "task-notification"})]
    fake = _run_relay(monkeypatch, tmp_path, lines, ["run-a", "run-a"])
    assert len(fake.adds) == 2
    assert all("line" in f for _k, f in fake.adds)
