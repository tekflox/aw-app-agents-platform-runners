"""`POST /abort` — the route agents-platform-multitenant calls to stop a
Runner-backed run (Kanban bug:abort-does-not-propagate-to-runner-backed-run).

The kill itself is covered by tests/test_abort_job.py; this file pins the
route's contract: its auth, what it rejects, and the fact that it answers 200
for a run that was already gone.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import shared_redis  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


def _client(monkeypatch, calls, result=None):
    monkeypatch.setattr(
        execute_mod, "abort_job",
        lambda run_id, redis_url=None, **kw: (
            calls.append({"run_id": run_id, "redis_url": redis_url, **kw}),
            result or {"run_id": run_id, "status": "not_found"},
        )[1])
    return TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))


def test_abort_requires_the_shared_secret(monkeypatch):
    """Same gate as /execute — and it runs BEFORE the body is looked at, so an
    unauthenticated caller learns nothing about what this route accepts."""
    calls: list = []
    resp = _client(monkeypatch, calls).post("/abort", json={})

    assert resp.status_code == 401
    assert calls == []


def test_abort_without_a_configured_secret_is_a_500(monkeypatch):
    calls: list = []
    monkeypatch.setattr(execute_mod, "abort_job", lambda *a, **k: calls.append(a))
    client = TestClient(build_routes({"shared_redis_url": "redis://example.test:6379/0"}))
    resp = client.post("/abort", headers={"x-runner-secret": "s3cr3t"}, json={"run_id": "r"})

    assert resp.status_code == 500
    assert calls == []


def test_a_body_without_a_run_id_is_rejected(monkeypatch):
    calls: list = []
    resp = _client(monkeypatch, calls).post(
        "/abort", headers={"x-runner-secret": "s3cr3t"}, json={})

    assert resp.status_code == 400
    assert "run_id is required" in resp.json()["detail"]
    assert calls == []


def test_an_abort_for_a_finished_run_is_200_not_404(monkeypatch):
    """RunnerLLM retries 404 as a transient app-reload/tunnel failure, so a
    404 here would make it hammer the abort three times over three seconds for
    a run that was simply already done — the exact race this card reports."""
    calls: list = []
    resp = _client(monkeypatch, calls).post(
        "/abort", headers={"x-runner-secret": "s3cr3t"}, json={"run_id": "run-x"})

    assert resp.status_code == 200
    assert resp.json() == {"run_id": "run-x", "status": "not_found"}


def test_the_warm_hints_are_forwarded(monkeypatch):
    """agent_id/session_id are optional and only the warm path can use them —
    a warm container's name is keyed on that pair, not on the run id."""
    calls: list = []
    _client(monkeypatch, calls).post(
        "/abort", headers={"x-runner-secret": "s3cr3t"},
        json={"run_id": "run-y", "agent_id": "a1", "session_id": "s1"})

    assert calls == [{"run_id": "run-y", "redis_url": "redis://example.test:6379/0",
                      "agent_id": "a1", "session_id": "s1"}]


def test_a_killed_run_is_reported_as_killed(monkeypatch):
    calls: list = []
    client = _client(monkeypatch, calls,
                     result={"run_id": "run-z", "status": "killed",
                             "container": "aw-warm-a-b", "resolved_by": "Redis registry"})
    resp = client.post("/abort", headers={"x-runner-secret": "s3cr3t"},
                       json={"run_id": "run-z"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "killed"


def test_abort_still_runs_when_no_shared_redis_can_be_resolved(monkeypatch):
    """Degraded, not refused: this worker's own registry and the deterministic
    container names still resolve, and abort's whole job is to stop something
    that is costing money. /execute 500s in this situation because a run with
    nowhere to publish is useless; an abort is not."""
    calls: list = []
    monkeypatch.setattr(shared_redis, "resolve", lambda _cfg: None)
    monkeypatch.setattr(
        execute_mod, "abort_job",
        lambda run_id, redis_url=None, **kw: (
            calls.append({"run_id": run_id, "redis_url": redis_url, **kw}),
            {"run_id": run_id, "status": "not_found"},
        )[1])
    client = TestClient(build_routes({"execute_secret": "s3cr3t"}))
    resp = client.post("/abort", headers={"x-runner-secret": "s3cr3t"},
                       json={"run_id": "run-w"})

    assert resp.status_code == 200
    assert calls[0]["redis_url"] is None


def test_execute_still_enforces_the_same_secret_through_the_shared_dependency(monkeypatch):
    """The check moved out of execute_job's body into a dependency both routes
    share — /execute's own gate must be exactly as it was."""
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")
    spawned: list = []
    monkeypatch.setattr(execute_mod, "start_job",
                        lambda job, redis_url: spawned.append(job) or True)
    client = TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))

    assert client.post("/execute", json={"prompt": "hi"}).status_code == 401
    assert client.post("/execute", headers={"x-runner-secret": "wrong"},
                       json={"prompt": "hi"}).status_code == 401
    assert spawned == []

    ok = client.post("/execute", headers={"x-runner-secret": "s3cr3t"},
                     json={"prompt": "hi"})
    assert ok.status_code == 200
    assert len(spawned) == 1
