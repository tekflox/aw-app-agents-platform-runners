"""Redis-backed observability counters for the execution-index hook (Kanban
bug:execution-index-hook-silently-drops-most-runs) — attempted/indexed/
skipped:<reason> on index_run()'s decision branches, and the
warm_watchers_started/resolved pair that fingerprints the dominant root
cause: a warm-dispatch watcher thread silently killed by a restart of this
(Tier-1, in-process) app before it ever resolves.

Run: .venv/aw/bin/python -m pytest tests/test_execution_index_counters.py
"""
from __future__ import annotations

import fnmatch
import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execution_index as idx  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


class _FakePipeline:
    """Minimal stand-in for redis.Redis.pipeline() — enough for ``_incr``'s
    INCR lifetime key + INCR/EXPIRE bucket key dual write. No real
    batching/atomicity; just records ops and applies them against the same
    FakeCounterRedis on ``execute()``, same as the real pipeline resolving
    against the same keyspace."""

    def __init__(self, redis: "FakeCounterRedis"):
        self._redis = redis
        self._ops: list[tuple] = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incr":
                results.append(self._redis.incr(op[1]))
            else:
                self._redis.expirations[op[1]] = op[2]
                results.append(True)
        self._ops = []
        return results


class FakeCounterRedis:
    """Minimal stand-in for the sync redis client's INCR/MGET/pipeline —
    real Redis semantics: INCR stores (and MGET returns) a string, even
    though INCR's own reply is an integer."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def incr(self, key):
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    def mget(self, keys):
        return [self.data.get(k) for k in keys]

    def scan_iter(self, match=None):
        if match is None:
            return list(self.data.keys())
        return [k for k in self.data if fnmatch.fnmatch(k, match)]

    def pipeline(self):
        return _FakePipeline(self)

    def hsetnx(self, key, field, value):
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = value
        return 1

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)


def _window_key(name: str, hours_ago: int = 0) -> str:
    """The hourly bucket key ``_incr``/``counters_window_snapshot`` use for
    ``name`` at ``hours_ago`` hours before now — lets a test seed/assert on
    the window without duplicating the hour-bucket format."""
    return idx._WINDOW_BUCKET_PREFIX + name + ":" + idx._hour_bucket(time.time() - hours_ago * 3600)


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


def test_kb_post_gets_its_own_longer_timeout_without_touching_the_shared_client(monkeypatch):
    """Kanban observability:execution-index-skipped-other-unclassified: the
    KB POST used to inherit the shared client's timeout=5, measured live at
    18s+ for a single trivial chunk. It must now carry its own override.
    The shared client itself must still default to 5 — that default is the
    floor every future call site inherits unless it opts into its own
    override, same as this POST and the AP-MT GETs below."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    captured = {}
    original_post = httpx.Client.post

    def spy_post(self, url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        assert self.timeout == httpx.Timeout(5)
        return original_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", spy_post)

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

    assert captured["timeout"] == idx.KB_POST_TIMEOUT_S
    assert captured["timeout"] > 5


def test_ap_mt_gets_carry_a_30s_read_budget_but_a_5s_connect_budget(monkeypatch):
    """resilience:execution-index-apmt-get-timeout-5s (Kanban 3fc5bf3b-9510-
    8158-800a-ea32a7be662e) — the two AP-MT GETs (run poll, events) used to
    share the client's flat timeout=5 for every phase, and a sequential
    backfill measured 4 live httpx.ReadTimeout under load. The fix is a
    per-call override, AP_MT_GET_TIMEOUT, that raises only the read phase;
    connect must stay at 5s so a genuinely unreachable AP-MT still fails
    fast. httpx normalizes a Timeout into request.extensions["timeout"] as a
    dict of connect/read/write/pool floats — assert both GETs carry it."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    captured_timeouts = []

    def handler(request):
        captured_timeouts.append((request.url.path, request.extensions["timeout"]))
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        return httpx.Response(200, json={"ok": True})

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is True

    get_timeouts = [t for path, t in captured_timeouts if path.startswith("/api/runs")]
    assert len(get_timeouts) == 2  # the run-poll GET and the events GET
    for timeout in get_timeouts:
        assert timeout["connect"] == 5.0
        assert timeout["read"] == 30.0

    post_timeouts = [t for path, t in captured_timeouts if path.endswith("/executions")]
    assert len(post_timeouts) == 1
    assert post_timeouts[0]["read"] == 120.0


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


def test_other_counter_for_a_network_level_failure_is_named_by_exception_type(monkeypatch):
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
    # The undifferentiated bucket never gets written to anymore — this is
    # the exact defect the Kanban card reported (200/266 runs anonymous).
    # It now also names the call site: this ConnectError happens on the KB
    # POST, so it lands in skipped:kb_post:ConnectError, not skipped:other.
    assert snapshot["skipped:kb_post:ConnectError"] == 1
    assert snapshot.get("skipped:other", 0) == 0
    assert snapshot.get("skipped:other:ConnectError", 0) == 0
    assert snapshot["skipped:http_error"] == 0


def test_two_distinct_exception_types_get_two_distinct_named_buckets(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler_connect_error(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        raise httpx.ConnectError("connection refused", request=request)

    def handler_value_error(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json="not-a-list")
        return httpx.Response(200, json=_run())

    idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://dead-kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler_connect_error))
    idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler_value_error))

    snapshot = idx.counters_snapshot()
    # The POST-side ConnectError is phase-attributed; the events-not-a-list
    # ValueError happens outside all three phases' own try/excepts, so it
    # still falls back to the generic skipped:other bucket.
    assert snapshot["skipped:kb_post:ConnectError"] == 1
    assert snapshot["skipped:other:ValueError"] == 1


def test_apmt_get_run_readtimeout_is_attributed_to_its_phase(monkeypatch):
    """Kanban observability:execution-index-readtimeout-has-no-call-site:
    a ReadTimeout from the run-detail poll GET must name that call site,
    not just the exception type, so it is distinguishable from the same
    exception raised by the other two HTTP calls."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.url.path == "/api/runs/run-1":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_run())

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:apmt_get_run:ReadTimeout"] == 1
    assert snapshot.get("skipped:other:ReadTimeout", 0) == 0

    # Enough eligible volume (>= _FAILURE_RATIO_MIN_SAMPLE) for the doctor
    # to judge by, with the named failure above the 0.2 threshold — proves
    # the prefix rule counts this phase-attributed key as a failure, not
    # as skipped:gate, with no change to routes.py.
    fake.data[_window_key("attempted")] = "10"
    fake.data[_window_key("skipped:apmt_get_run:ReadTimeout")] = "3"
    fake.data[_window_key("indexed")] = "7"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is False
    assert body["detail"]["counters_window"]["skipped:apmt_get_run:ReadTimeout"] == 3
    assert any("3/10" in p for p in body["detail"]["problems"])


def test_apmt_get_events_readtimeout_is_attributed_to_its_phase(monkeypatch):
    """Same as above for the events GET — the second of the three calls."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.url.path.endswith("/events"):
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_run())

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:apmt_get_events:ReadTimeout"] == 1
    assert snapshot.get("skipped:other:ReadTimeout", 0) == 0

    fake.data[_window_key("attempted")] = "10"
    fake.data[_window_key("skipped:apmt_get_events:ReadTimeout")] = "3"
    fake.data[_window_key("indexed")] = "7"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is False
    assert body["detail"]["counters_window"]["skipped:apmt_get_events:ReadTimeout"] == 3
    assert any("3/10" in p for p in body["detail"]["problems"])


def test_kb_post_readtimeout_is_attributed_to_its_phase(monkeypatch):
    """Same as above for the KB POST — the slow, 120s-budget third call,
    which is wrapped by its own try/except (for the secret-reject/
    http_error split) so a ReadTimeout there needs its own except branch
    rather than falling through to the generic catch-all."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        raise httpx.ReadTimeout("timed out", request=request)

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:kb_post:ReadTimeout"] == 1
    assert snapshot.get("skipped:other:ReadTimeout", 0) == 0

    fake.data[_window_key("attempted")] = "10"
    fake.data[_window_key("skipped:kb_post:ReadTimeout")] = "3"
    fake.data[_window_key("indexed")] = "7"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()
    assert body["ok"] is False
    assert body["detail"]["counters_window"]["skipped:kb_post:ReadTimeout"] == 3
    assert any("3/10" in p for p in body["detail"]["problems"])


def test_exception_outside_the_three_phases_still_falls_back_to_skipped_other(monkeypatch):
    """A swallowed exception raised outside index_run's three HTTP calls
    (here, the events-not-a-list ValueError) must stay anonymous-fallback
    generic, not silently disappear now that the three phases have their
    own named buckets."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json="not-a-list")
        return httpx.Response(200, json=_run())

    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler)) is False

    snapshot = idx.counters_snapshot()
    assert snapshot["skipped:other:ValueError"] == 1
    assert snapshot.get("skipped:apmt_get_events:ValueError", 0) == 0


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
    monkeypatch.setattr(idx, "index_run", lambda run_id, **_kw: None)
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
    assert set(body.keys()) == set(idx.COUNTER_NAMES) | {"retry_queue"}


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


def test_execution_index_doctor_ok_with_high_gate_ratio_and_zero_failures(monkeypatch):
    """Acceptance test #1 (card 3fc5bf3b): skipped:gate is the
    _interesting() gate's intentional exclusion, not a failure — a window
    dominated by gate exclusions (562/935 live) must not trip the doctor
    just because overall attempted vs indexed looks skewed."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[_window_key("attempted")] = "100"
    fake.data[_window_key("skipped:gate")] = "94"
    fake.data[_window_key("indexed")] = "6"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is True
    assert body["detail"]["problems"] == []


def test_execution_index_doctor_flags_real_failures_above_threshold_in_the_window(monkeypatch):
    """Acceptance test #2: a real failure rate (not gate exclusions) above
    the 0.2 threshold must flag, and the problem message must name the
    ratio."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[_window_key("attempted")] = "100"
    fake.data[_window_key("skipped:http_error")] = "30"
    fake.data[_window_key("indexed")] = "70"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is False
    assert any("30/100" in p and "30%" in p for p in body["detail"]["problems"])


def test_execution_index_doctor_ignores_historical_lifetime_pollution_with_a_clean_window(monkeypatch):
    """The historical-contamination regression test: a legacy lifetime
    skipped:other of 208 (pre-v0.173.0 catch-all pollution) must not leak
    into a doctor verdict that now reads the window, not the lifetime
    counters."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[idx._COUNTER_PREFIX + "skipped:other"] = "208"
    fake.data[_window_key("attempted")] = "10"
    fake.data[_window_key("indexed")] = "10"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is True


def test_execution_index_doctor_counts_a_dynamic_skipped_other_bucket_in_the_window(monkeypatch):
    """A skipped:other:<ExceptionType> bucket discovered dynamically must
    count in the window numerator with no code change — the whole point of
    the prefix rule."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[_window_key("attempted")] = "20"
    fake.data[_window_key("skipped:other:ReadTimeout")] = "10"
    fake.data[_window_key("indexed")] = "10"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is False
    assert body["detail"]["counters_window"]["skipped:other:ReadTimeout"] == 10


def test_execution_index_doctor_does_not_count_in_flight_runs_as_failures(monkeypatch):
    """attempted increments at the top of index_run before any outcome is
    known — a run still in flight (no skipped:*/indexed counter yet) must
    not be treated as a failure just because eligible - indexed is
    nonzero. The numerator is the explicit skipped:* sum, not a
    subtraction."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[_window_key("attempted")] = "10"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is True
    assert body["detail"]["problems"] == []


def test_execution_index_doctor_notes_insufficient_volume_below_min_sample(monkeypatch):
    """A fresh/low-traffic window — e.g. 4 eligible runs, all failed — is a
    100% failure ratio by arithmetic alone but must not flag with no real
    sample size behind it; it must still surface an explicit note."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.data[_window_key("attempted")] = "4"
    fake.data[_window_key("skipped:http_error")] = "4"

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/doctor").json()

    assert body["ok"] is True
    assert "insufficient volume" in body["detail"]["note"]


def test_incr_sets_a_ttl_on_the_window_bucket_key(monkeypatch):
    """No unbounded key growth: every window bucket write carries an
    EXPIRE in the same pipeline as the INCR."""
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    idx._incr("attempted")

    bucket_key = _window_key("attempted")
    assert fake.data[bucket_key] == "1"
    assert fake.expirations[bucket_key] == idx._WINDOW_BUCKET_TTL_S
    # Lifetime key also incremented in the same dual write.
    assert fake.data[idx._COUNTER_PREFIX + "attempted"] == "1"


def test_execution_index_doctor_ok_when_nothing_has_happened_yet(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/doctor")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["detail"]["counters"] == {name: 0 for name in idx.COUNTER_NAMES}
    assert body["detail"]["counters_window"] == {name: 0 for name in idx.COUNTER_NAMES}
    assert body["detail"]["window_hours"] == idx.WINDOW_LOOKBACK_HOURS
    assert body["detail"]["problems"] == []
    assert "insufficient volume" in body["detail"]["note"]


def test_execution_index_doctor_reports_not_ok_when_redis_is_unavailable():
    # No override — conftest's default _counter_redis_client already
    # returns None, matching a deployment with no discoverable Redis.
    client = TestClient(build_routes({}))
    resp = client.get("/execution-index/doctor")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# --- Retry queue (card 3fc5bf3b-9510-81ec-8752-fc06d82a8a5e) ---------------
#
# Live traffic loses ~19% of eligible runs to the warm watcher's one-shot
# index_run() call discarding any skipped:* outcome, while a synchronous
# backfill re-running the same index_run() loses only 0.1%. These tests
# cover the retry-queue decision (queue a real failure, never a legitimate
# skipped:gate exclusion) and the hourly sweep that drains it.


def test_index_and_maybe_queue_retry_queues_a_real_failure(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    ok = idx._index_and_maybe_queue_retry("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler))

    assert ok is False
    # The original skipped:<reason> must survive alongside the attempt
    # count — the explicit QA-accountability condition on this card: a
    # retry that drains silently trades a visible loss for an invisible one.
    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {"run-1": "0:skipped:apmt_get_run:ReadTimeout"}


def test_index_and_maybe_queue_retry_does_not_queue_a_gate_exclusion(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_run(output="short"))

    ok = idx._index_and_maybe_queue_retry("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler))

    assert ok is False
    assert idx._RETRY_QUEUE_KEY not in fake.hashes


def test_index_and_maybe_queue_retry_does_not_queue_on_success(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        return httpx.Response(200, json={"ok": True})

    ok = idx._index_and_maybe_queue_retry("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler))

    assert ok is True
    assert idx._RETRY_QUEUE_KEY not in fake.hashes


def test_queue_retry_does_not_reset_an_existing_attempt_count_or_reason(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-1": "7:skipped:finalize_timeout"}

    idx._queue_retry("run-1", "skipped:http_error")

    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {"run-1": "7:skipped:finalize_timeout"}


def test_sweep_retry_queue_removes_entry_on_success(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-ok": "0:skipped:http_error"}
    monkeypatch.setattr(idx, "index_run", lambda run_id, **_kw: True)

    result = idx.sweep_retry_queue()

    assert result == {"queued": 1, "indexed": 1, "gated": 0, "requeued": 0, "exhausted": 0}
    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {}


def test_sweep_retry_queue_removes_entry_on_a_legitimate_gate_exclusion(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-gated": "3:skipped:http_error"}

    def fake_index_run(run_id, *, on_skip=None, **_kw):
        if on_skip is not None:
            on_skip("skipped:gate")
        return False

    monkeypatch.setattr(idx, "index_run", fake_index_run)

    result = idx.sweep_retry_queue()

    assert result["gated"] == 1
    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {}


def test_sweep_retry_queue_requeues_a_repeat_failure_preserving_the_original_reason(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-stuck": "5:skipped:finalize_timeout"}

    # A different failure on THIS retry attempt — the stored reason must
    # stay the ORIGINAL one that first queued the run, not this one.
    def fake_index_run(run_id, *, on_skip=None, **_kw):
        if on_skip is not None:
            on_skip("skipped:http_error")
        return False

    monkeypatch.setattr(idx, "index_run", fake_index_run)

    result = idx.sweep_retry_queue()

    assert result["requeued"] == 1
    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {"run-stuck": "6:skipped:finalize_timeout"}


def test_sweep_retry_queue_gives_up_after_max_attempts(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {
        "run-dead": f"{idx._RETRY_MAX_ATTEMPTS - 1}:skipped:other:ConnectError"}

    def fake_index_run(run_id, *, on_skip=None, **_kw):
        if on_skip is not None:
            on_skip("skipped:other:ConnectError")
        return False

    monkeypatch.setattr(idx, "index_run", fake_index_run)

    result = idx.sweep_retry_queue()

    assert result["exhausted"] == 1
    assert fake.hashes[idx._RETRY_QUEUE_KEY] == {}
    snapshot = idx.counters_snapshot()
    assert snapshot["retry_exhausted"] == 1


def test_retry_queue_snapshot_reports_attempts_and_reason(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-stuck": "2:skipped:finalize_timeout"}

    assert idx.retry_queue_snapshot() == {
        "run-stuck": {"attempts": 2, "reason": "skipped:finalize_timeout"}}


def test_retry_queue_snapshot_is_none_when_redis_unavailable():
    # No override — conftest's default _counter_redis_client returns None.
    assert idx.retry_queue_snapshot() is None


def test_execution_index_status_endpoint_reports_the_retry_queue(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)
    fake.hashes[idx._RETRY_QUEUE_KEY] = {"run-stuck": "1:skipped:http_error"}

    client = TestClient(build_routes({}))
    body = client.get("/execution-index/status").json()

    assert body["retry_queue"] == {"run-stuck": {"attempts": 1, "reason": "skipped:http_error"}}


def test_sweep_retry_queue_is_a_noop_when_queue_is_empty(monkeypatch):
    fake = FakeCounterRedis()
    monkeypatch.setattr(idx, "_counter_redis_client", lambda: fake)

    result = idx.sweep_retry_queue()

    assert result == {"queued": 0, "indexed": 0, "gated": 0, "requeued": 0, "exhausted": 0}


def test_sweep_retry_queue_returns_zeros_when_redis_unavailable():
    # No override — conftest's default _counter_redis_client returns None.
    result = idx.sweep_retry_queue()

    assert result == {"queued": 0, "indexed": 0, "gated": 0, "requeued": 0, "exhausted": 0}
