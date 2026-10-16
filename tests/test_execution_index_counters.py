"""Redis-backed observability counters for the execution-index hook (Kanban
bug:execution-index-hook-silently-drops-most-runs) — attempted/indexed/
skipped:<reason> on index_run()'s decision branches, and the
warm_watchers_started/resolved pair that fingerprints the dominant root
cause: a warm-dispatch watcher thread silently killed by a restart of this
(Tier-1, in-process) app before it ever resolves.

Run: .venv/aw/bin/python -m pytest tests/test_execution_index_counters.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execution_index as idx  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


class FakeCounterRedis:
    """Minimal stand-in for the sync redis client's INCR/MGET — real Redis
    semantics: INCR stores (and MGET returns) a string, even though INCR's
    own reply is an integer."""

    def __init__(self):
        self.data: dict[str, str] = {}

    def incr(self, key):
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    def mget(self, keys):
        return [self.data.get(k) for k in keys]


def _run(ended_at="2026-09-04T10:00:00Z", **overrides):
    row = {
        "id": "run-1", "status": "success", "ended_at": ended_at,
        "started_at": "2026-09-04T09:00:00Z", "input": "diagnose it",
        "output": "resolved " * 40, "source_slug": "coder", "target_slug": "target",
    }
    row.update(overrides)
    return row


def test_counters_snapshot_is_none_when_redis_unavailable():
    # No override here — the suite-wide conftest fixture already forces
    # _counter_redis_client to return None, exactly like a deployment with
    # no discoverable shared Redis.
    snapshot = idx.counters_snapshot()
    assert all(v is None for v in snapshot.values())


def test_attempted_and_gate_counters_increment(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_run(output="short"))

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["attempted"] == 1
    assert snapshot["skipped:gate"] == 1
    assert snapshot["indexed"] == 0


def test_indexed_counter_increments_on_success(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        return httpx.Response(200, json={"ok": True})

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is True

    snapshot = idx.counters_snapshot()
    assert snapshot["attempted"] == 1
    assert snapshot["indexed"] == 1
    assert snapshot["skipped:gate"] == 0


def test_finalize_timeout_counter_increments(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        return httpx.Response(200, json=_run(ended_at=None, status="running"))

    clock = iter([0, 61])
    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "all",
    }, transport=httpx.MockTransport(handler), sleep=lambda _n: None,
       monotonic=lambda: next(clock)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:finalize_timeout"] == 1
    assert snapshot["indexed"] == 0


def test_secret_reject_counter_is_distinct_from_generic_http_error(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        return httpx.Response(401, json={"error": "bad secret"})

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:secret_reject"] == 1
    assert snapshot["skipped:http_error"] == 0


def test_http_error_counter_for_a_non_secret_kb_failure(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        return httpx.Response(500, json={"error": "kb down"})

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:http_error"] == 1
    assert snapshot["skipped:secret_reject"] == 0


def test_other_counter_for_a_network_level_failure(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        raise httpx.ConnectError("connection refused", request=request)

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://dead-kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:other"] == 1
    assert snapshot["skipped:http_error"] == 0


def _install_fake_redis_module(monkeypatch, xread):
    class FakeStreamRedis:
        def close(self):
            pass

    FakeStreamRedis.xread = xread

    class RedisModule:
        class Redis:
            @staticmethod
            def from_url(*_a, **_k):
                return FakeStreamRedis()

    monkeypatch.setitem(sys.modules, "redis", RedisModule)


def test_warm_watchers_started_and_resolved_both_increment_on_success(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    idx.configure({"execution_index_mode": "all"})
    monkeypatch.setattr(idx, "index_run", lambda run_id: None)
    _install_fake_redis_module(
        monkeypatch, lambda self, *_a, **_k: [("stream", [("1-0", {"done": "1"})])])

    thread = idx.start_after_stream_done("warm-1", "redis://fake")
    thread.join(timeout=1)

    snapshot = idx.counters_snapshot()
    assert snapshot["warm_watchers_started"] == 1
    assert snapshot["warm_watchers_resolved"] == 1


def test_warm_watcher_gap_visible_when_resolution_never_runs(monkeypatch):
    """THE regression test for root cause #1: simulate a watcher thread that
    never reaches any of _wait()'s three resolution branches (in production,
    the whole process is killed by a restart before ``xread`` ever returns —
    here the fake client's ``xread`` just blocks on an Event nobody sets).
    ``warm_watchers_started`` is incremented synchronously in the calling
    thread, right before ``thread.start()`` — so the assertion below does
    not race the background thread at all, and the gap it proves is exactly
    the fingerprint this fix exists to make visible."""
    block_forever = threading.Event()

    def _blocking_xread(self, *_a, **_k):
        block_forever.wait()
        return []

    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    idx.configure({"execution_index_mode": "all"})
    _install_fake_redis_module(monkeypatch, _blocking_xread)

    thread = idx.start_after_stream_done("warm-stuck", "redis://fake")
    assert thread is not None

    snapshot = idx.counters_snapshot()
    assert snapshot["warm_watchers_started"] == 1
    assert snapshot["warm_watchers_resolved"] == 0


def test_warm_watchers_resolved_increments_on_the_deadline_branch(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    idx.configure({"execution_index_mode": "all"})

    # Deadline branch: xread never produces a "done" entry, and the 24h
    # monotonic deadline (call 1) is already behind the while-check's own
    # monotonic() read (call 2) — exits the loop without ever calling xread.
    clock = iter([0, 100000])
    monkeypatch.setattr(idx.time, "monotonic", lambda: next(clock))
    _install_fake_redis_module(monkeypatch, lambda self, *_a, **_k: [])

    thread = idx.start_after_stream_done("warm-deadline", "redis://fake")
    thread.join(timeout=1)

    snapshot = idx.counters_snapshot()
    assert snapshot["warm_watchers_started"] == 1
    assert snapshot["warm_watchers_resolved"] == 1


def test_execution_index_status_endpoint_reports_the_raw_counters(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[idx._COUNTER_PREFIX + "attempted"] = "5"
    fake.data[idx._COUNTER_PREFIX + "indexed"] = "3"

    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["attempted"] == 5
    assert body["indexed"] == 3
    assert set(body.keys()) == set(idx.COUNTER_NAMES)


def test_execution_index_doctor_flags_a_persistent_warm_watcher_gap(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[idx._COUNTER_PREFIX + "warm_watchers_started"] = "30"
    fake.data[idx._COUNTER_PREFIX + "warm_watchers_resolved"] = "1"

    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/doctor")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert any("warm_watchers_started" in p for p in body["detail"]["problems"])


def test_execution_index_doctor_flags_a_high_skip_ratio_with_enough_volume(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[idx._COUNTER_PREFIX + "attempted"] = "40"
    fake.data[idx._COUNTER_PREFIX + "indexed"] = "5"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is False
    assert any("skipped" in p for p in body["detail"]["problems"])


def test_execution_index_doctor_ignores_skip_ratio_below_the_sample_floor(monkeypatch):
    """A fresh/low-traffic deployment with e.g. 1 attempted and 0 indexed is
    a 100% skip ratio by arithmetic alone — must not flag on that with no
    real sample size behind it."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[idx._COUNTER_PREFIX + "attempted"] = "1"
    fake.data[idx._COUNTER_PREFIX + "indexed"] = "0"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is True


def test_execution_index_doctor_ok_when_nothing_has_happened_yet(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/doctor")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": {
        "counters": {name: 0 for name in idx.COUNTER_NAMES}, "problems": []}}


def test_execution_index_doctor_reports_not_ok_when_redis_is_unavailable():
    # No override — conftest's default _counter_redis_client already
    # returns None, matching a deployment with no discoverable Redis.
    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/doctor")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
