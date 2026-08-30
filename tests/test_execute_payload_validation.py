"""/execute must refuse a job it cannot run, instead of spending a container on it.

Until 2026-08-30 this route accepted ANY body. `{}` passed straight through to
start_job: `prompt` defaulted to "", `cli` defaulted to "claude", and a real
cold container was spawned to run an agent on an empty prompt. It answered
`{"status": "started"}`, so nothing anywhere said a thing was wrong.

Found by probing whether /execute was reachable at all after a dispatch 404'd —
the probe POSTed `{}` and started run f7122833. A health probe should not be
able to bill a cold start.

The two job shapes are mutually exclusive by construction: execute.py's
_build_container_kwargs branches on raw_command before it ever reads prompt, so
a body carrying both silently discards the prompt. That is rejected too rather
than resolved by precedence — the caller has a bug either way, and only one of
the two readings is the one it meant.

Ordering matters and is asserted: the secret check still runs FIRST, so an
unauthenticated caller learns nothing about which bodies this route would
accept.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


def _client(monkeypatch, spawned):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")
    monkeypatch.setattr(execute_mod, "_run_job_blocking",
                        lambda job, redis_url: spawned.append(job))
    return TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))


def _post(client, body):
    return client.post("/execute", headers={"x-runner-secret": "s3cr3t"}, json=body)


def test_an_empty_body_is_rejected_and_spawns_nothing(monkeypatch):
    """The exact probe that started run f7122833."""
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned), {})

    assert resp.status_code == 400
    assert "nothing to execute" in resp.json()["detail"]
    assert spawned == []


def test_a_whitespace_only_prompt_is_rejected(monkeypatch):
    """An empty string and a string of spaces produce the same useless run."""
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned), {"prompt": "   \n "})

    assert resp.status_code == 400
    assert spawned == []


def test_a_non_string_prompt_is_rejected(monkeypatch):
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned), {"prompt": {"text": "hi"}})

    assert resp.status_code == 400
    assert spawned == []


def test_a_body_carrying_both_shapes_is_rejected_rather_than_resolved(monkeypatch):
    """raw_command would win and the prompt would vanish — say so."""
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned),
                 {"prompt": "hi", "raw_command": "echo hi"})

    assert resp.status_code == 400
    assert "both" in resp.json()["detail"]
    assert spawned == []


def test_a_body_that_is_not_an_object_is_rejected(monkeypatch):
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned), ["not", "a", "dict"])

    assert resp.status_code == 400
    assert spawned == []


def test_malformed_json_is_a_400_not_a_500(monkeypatch):
    """`await request.json()` raises on a bad body; unhandled that surfaces as
    a 500, which tells the caller "my fault" about the caller's own bug."""
    spawned: list = []
    client = _client(monkeypatch, spawned)
    resp = client.post("/execute", headers={"x-runner-secret": "s3cr3t",
                                            "content-type": "application/json"},
                       content=b"{not json")

    assert resp.status_code == 400
    assert spawned == []


def test_a_real_agent_job_still_runs(monkeypatch):
    """The guard must not touch the path everything actually uses."""
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned), {"prompt": "do the thing"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    assert len(spawned) == 1
    assert spawned[0]["prompt"] == "do the thing"


def test_a_monitor_raw_command_job_still_runs(monkeypatch):
    """monitor_run.py sends raw_command and NO prompt — the other live caller."""
    spawned: list = []
    resp = _post(_client(monkeypatch, spawned),
                 {"raw_command": "df -h", "cwd": "/tmp", "timeout_seconds": 30})

    assert resp.status_code == 200
    assert len(spawned) == 1
    assert spawned[0]["raw_command"] == "df -h"


def test_the_secret_is_still_checked_before_the_body(monkeypatch):
    """A caller without the secret gets 401, not a 400 that would confirm the
    endpoint exists and describe its schema."""
    spawned: list = []
    client = _client(monkeypatch, spawned)
    resp = client.post("/execute", headers={"x-runner-secret": "wrong"}, json={})

    assert resp.status_code == 401
    assert spawned == []
