"""Regression test for card 3d15bf3b-9510-8164-95c8-d1c26da0df00's blocking
bug: the codex-retry refactor of `_run_job_blocking`'s cold-agent path left
`kill_timer.cancel()` + `_publish_done(...)` sitting AFTER the call to
`_run_cold_agent_with_retry` instead of inside a `finally`. If that call
raised — e.g. Redis dropping mid-flush, exactly the failure
`_publish_line`/`_publish_done` each wrap their own XADD in try/except to
survive — the function returned without ever publishing the `done` sentinel
and with the kill timer still armed: the run looks hung forever from
agents-platform's side, and a stray kill timer fires later.

This pins the fix (`try/except/finally` around the call, execute.py's
`_run_job_blocking`) by forcing `_run_cold_agent_with_retry` to raise and
asserting `_publish_done`, `execution_index.start` and `r.close()` still all
fire. Run against the pre-fix code (cleanup after the call, no finally) this
test fails with the injected RuntimeError propagating out of
`_run_job_blocking` uncaught, before any of those assertions are reached.

Run: .venv/aw/bin/python -m pytest tests/test_execute_cleanup_on_retry_failure.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


class _FakeContainer:
    pass


class _FakeImages:
    def pull(self, image):
        pass


class _FakeContainers:
    def run(self, image, **kwargs):
        return _FakeContainer()


class _FakeDockerClient:
    def __init__(self, base_url=None):
        self.images = _FakeImages()
        self.containers = _FakeContainers()


class _FakeRedis:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _install_fake_docker_module(monkeypatch):
    fake_docker = types.ModuleType("docker")
    fake_docker.DockerClient = _FakeDockerClient
    fake_docker.errors = types.SimpleNamespace(APIError=Exception, ImageNotFound=Exception)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)


def test_publish_done_and_cleanup_fire_even_if_the_retry_loop_raises(monkeypatch):
    _install_fake_docker_module(monkeypatch)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/fake.sock")

    fake_redis = _FakeRedis()
    monkeypatch.setattr(execute_mod, "_redis_client", lambda url: fake_redis)
    monkeypatch.setattr(execute_mod, "_build_container_kwargs",
                         lambda job: ("img", ["codex"], {}, None))
    monkeypatch.setattr(execute_mod.warm_pool, "enabled", lambda: False)

    done_calls = []
    monkeypatch.setattr(execute_mod, "_publish_done",
                         lambda r, run_id, rc: done_calls.append((run_id, rc)))
    index_started = []
    monkeypatch.setattr(execute_mod.execution_index, "start",
                         lambda run_id: index_started.append(run_id))

    def _boom(*a, **k):
        raise RuntimeError("redis dropped mid-flush")
    monkeypatch.setattr(execute_mod, "_run_cold_agent_with_retry", _boom)

    job = {"run_id": "run-cleanup-1", "cli": "codex", "prompt": "hi"}
    execute_mod._run_job_blocking(job, "redis://example.test:6379/0")

    assert done_calls == [("run-cleanup-1", 1)]
    assert index_started == ["run-cleanup-1"]
    assert fake_redis.closed is True
