"""A repeated /execute for the same run_id must not start a second agent.

This is the safety half of a two-part fix (2026-08-23). agents-platform's
`RunnerLLM._dispatch` now RETRIES the handshake POST, because a single dropped
packet on the tunnel used to kill a turn before it started — the run ended as
an error with an empty reply and, on the Watch, as a frozen screen.

Retrying is only safe if this side is idempotent. From the caller's seat a lost
RESPONSE looks exactly like a lost REQUEST, so attempt 2 can perfectly well
arrive for a job that IS already running here. Without a guard both containers
publish into the same `run:{run_id}:events` stream: interleaved output, double
token cost, and two replies to one message.

The two halves have to move together. If this guard is ever removed, the retry
loop in runner.py must go with it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


RUN_ID = "9a1f20a2c93740dab553f2104b7686c0"


def _clear_started():
    with execute_mod._STARTED_RUN_IDS_LOCK:
        execute_mod._STARTED_RUN_IDS.clear()


def _client(monkeypatch, spawned):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")
    monkeypatch.setattr(execute_mod, "_run_job_blocking",
                        lambda job, redis_url: spawned.append(job.get("run_id")))
    return TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))


def _post(client, run_id):
    return client.post("/execute", headers={"x-runner-secret": "s3cr3t"},
                       json={"run_id": run_id, "prompt": "hi"})


def test_a_retried_dispatch_spawns_nothing_and_still_answers_ok(monkeypatch):
    """The retry case, end to end through the real route."""
    _clear_started()
    spawned: list = []
    client = _client(monkeypatch, spawned)

    first, second = _post(client, RUN_ID), _post(client, RUN_ID)

    # Both succeed: the caller's next step is identical either way (attach to
    # the run's Redis stream), so a duplicate is not an error to raise at it.
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == {"run_id": RUN_ID, "status": "started"}
    assert second.json() == {"run_id": RUN_ID, "status": "duplicate"}

    _join_spawned()
    assert spawned == [RUN_ID], f"expected exactly one agent, got {len(spawned)}"


def test_distinct_runs_are_never_confused_for_each_other(monkeypatch):
    """The guard is keyed on run_id, so real work still gets through."""
    _clear_started()
    spawned: list = []
    client = _client(monkeypatch, spawned)

    for rid in ("run-a", "run-b", "run-c"):
        assert _post(client, rid).json()["status"] == "started"

    _join_spawned()
    assert spawned == ["run-a", "run-b", "run-c"]


def test_the_guard_is_bounded_and_evicts_oldest_first(monkeypatch):
    """A long-lived process must not accumulate run ids forever. The window
    only has to outlive the caller's retry budget (seconds), not the run."""
    _clear_started()
    monkeypatch.setattr(execute_mod, "_run_job_blocking", lambda job, redis_url: None)
    monkeypatch.setattr(execute_mod, "_STARTED_RUN_IDS_MAX", 4)

    for i in range(10):
        execute_mod.start_job({"run_id": f"r{i}"}, "redis://x")

    _join_spawned()
    assert len(execute_mod._STARTED_RUN_IDS) == 4
    assert list(execute_mod._STARTED_RUN_IDS) == ["r6", "r7", "r8", "r9"]


def test_a_job_with_no_run_id_is_never_deduped(monkeypatch):
    """`run_id` is optional on the route (it mints one when absent). Missing
    ids must not all collide on a single empty-string key."""
    _clear_started()
    spawned: list = []
    monkeypatch.setattr(execute_mod, "_run_job_blocking",
                        lambda job, redis_url: spawned.append(job.get("run_id")))

    assert execute_mod.start_job({}, "redis://x") is True
    assert execute_mod.start_job({}, "redis://x") is True

    _join_spawned()
    assert len(spawned) == 2


def _join_spawned():
    """The route spawns real threads; wait for them before asserting."""
    for t in threading.enumerate():
        if t.name.startswith("runner-exec-") and t is not threading.current_thread():
            t.join(timeout=5)
