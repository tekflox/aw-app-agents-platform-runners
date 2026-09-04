import json

import httpx

from agents_platform_runners_app import execution_index as idx


def _run(ended_at="2026-09-04T10:00:00Z", **overrides):
    row = {
        "id": "run-1", "status": "success", "ended_at": ended_at,
        "started_at": "2026-09-04T09:00:00Z", "input": "diagnose it",
        "output": "resolved " * 40, "source_slug": "coder", "target_slug": "target",
        "session_id": "session", "notion_task_id": "card", "model_slug": "model",
        "cost_usd": 0.5, "tokens_in": 10, "tokens_out": 20, "error": None,
    }
    row.update(overrides)
    return row


def test_polls_until_run_is_finalized_then_posts_bounded_chunks():
    polls = 0
    posted = {}

    def handler(request):
        nonlocal polls
        if request.method == "GET" and request.url.path == "/api/runs/run-1":
            polls += 1
            return httpx.Response(200, json=_run(None if polls <= 3 else "done"))
        if request.method == "GET":
            assert request.url.params["limit"] == str(idx.EVENTS_LIMIT)
            return httpx.Response(200, json=[
                {"kind": "tool_call", "payload": {"name": "shell", "input": "ls"}},
                {"kind": "tool_result", "payload": {"name": "shell", "content": "ok"}},
            ])
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    now = iter([0, 0, .1, .2, .3])
    assert idx.index_run("run-1", config={
        "agents_platform_base": "http://ap", "kb_base_url": "http://kb",
        "execution_index_secret": "shared", "execution_index_mode": "interesting",
    }, transport=httpx.MockTransport(handler), sleep=lambda _n: None,
       monotonic=lambda: next(now))
    assert polls == 4
    assert posted["run_id"] == "run-1"
    assert 1 <= len(posted["chunks"]) <= 12
    assert all(len(c["content"]) <= 1500 for c in posted["chunks"])
    assert all(c["metadata"]["tool_names"] == ["shell"] for c in posted["chunks"])


def test_deadline_abandons_unfinalized_run_without_fetching_events_or_posting():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=_run(ended_at=None, status="running"))

    clock = iter([0, 61])
    assert idx.index_run("run-1", config={"agents_platform_base": "http://ap",
        "kb_base_url": "http://kb", "execution_index_mode": "all"},
        transport=httpx.MockTransport(handler), sleep=lambda _n: None,
        monotonic=lambda: next(clock)) is False
    assert paths == ["/api/runs/run-1"]


def test_events_at_explicit_limit_are_marked_truncated():
    payload = {}

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}] * idx.EVENTS_LIMIT)
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        payload.update(json.loads(request.content))
        return httpx.Response(200, json={})

    assert idx.index_run("run-1", config={"agents_platform_base": "http://ap",
        "kb_base_url": "http://kb", "execution_index_mode": "interesting"},
        transport=httpx.MockTransport(handler))
    assert payload["metadata_common"]["events_truncated"] is True
    assert all(c["metadata"]["events_truncated"] is True for c in payload["chunks"])


def test_redacts_every_required_secret_before_transport():
    leaked = " ".join([
        "sk-1234567890abcdef", "ghp_1234567890abcdef", "gho_1234567890abcdef",
        "ntn_1234567890abcdef", "secret_1234567890abcdef",
        "Bearer aaa.bbb.ccc", "X-Api-Key: very-secret-value",
    ])
    payload = {}

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call", "content": leaked}])
        if request.method == "GET":
            return httpx.Response(200, json=_run(input=leaked, output=leaked, error=leaked))
        payload.update(json.loads(request.content))
        return httpx.Response(200, json={})

    assert idx.index_run("run-1", config={"agents_platform_base": "http://ap",
        "kb_base_url": "http://kb", "execution_index_mode": "all"},
        transport=httpx.MockTransport(handler))
    wire = json.dumps(payload)
    for secret in leaked.split():
        if secret not in {"Bearer", "X-Api-Key:"}:
            assert secret not in wire
    assert wire.count("[REDACTED]") >= 7


def test_dead_kb_is_fail_open_after_run_fetch():
    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"kind": "tool_call"}])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        raise httpx.ConnectError("connection refused", request=request)

    assert idx.index_run("run-1", config={"agents_platform_base": "http://ap",
        "kb_base_url": "http://dead-kb", "execution_index_mode": "interesting"},
        transport=httpx.MockTransport(handler)) is False


def test_start_is_daemon_and_raw_monitor_is_skipped(monkeypatch):
    idx.configure({"execution_index_mode": "interesting"})
    called = []
    monkeypatch.setattr(idx, "index_run", lambda run_id: called.append(run_id))
    thread = idx.start("run-1")
    thread.join(timeout=1)
    assert thread.daemon is True
    assert called == ["run-1"]
    assert idx.start("raw-1", raw_command=True) is None


def test_interesting_mode_skips_short_run_without_tools_or_error():
    posted = []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[])
        if request.method == "GET":
            return httpx.Response(200, json=_run(output="short"))
        posted.append(request)
        return httpx.Response(200, json={})

    assert idx.index_run("run-1", config={"agents_platform_base": "http://ap",
        "kb_base_url": "http://kb", "execution_index_mode": "interesting"},
        transport=httpx.MockTransport(handler)) is False
    assert posted == []


def test_warm_stream_watcher_waits_for_done_then_indexes(monkeypatch):
    class FakeRedis:
        def xread(self, *_args, **_kwargs):
            return [("stream", [("1-0", {"line": "working"}),
                                 ("2-0", {"done": "1"})])]

        def close(self):
            pass

    class RedisModule:
        class Redis:
            @staticmethod
            def from_url(*_args, **_kwargs):
                return FakeRedis()

    import sys
    called = []
    idx.configure({"execution_index_mode": "all"})
    monkeypatch.setitem(sys.modules, "redis", RedisModule)
    monkeypatch.setattr(idx, "index_run", lambda run_id: called.append(run_id))
    thread = idx.start_after_stream_done("warm-1", "redis://fake")
    thread.join(timeout=1)
    assert thread.daemon is True
    assert called == ["warm-1"]
