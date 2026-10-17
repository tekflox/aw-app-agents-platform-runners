"""Dual-write to both execution-index homes during the aw-app-kb cutover.

docs/design/execution-index-migration-off-aw-app-kb.md §3.3 step 2. The
design's §6 names the exact bug these tests exist to prevent: a second target
added at ``index_run``'s CALL SITES instead of inside it, which silently
leaves the warm-watcher path, the retry sweep and the backfill script writing
to one home only — and the warm-watcher path carries most of this workspace's
live traffic.
"""
import json

import httpx

from agents_platform_runners_app import execution_index as idx


def _run(ended_at="2026-10-17T10:00:00Z", **overrides):
    row = {
        "id": "run-1", "status": "success", "ended_at": ended_at,
        "started_at": "2026-10-17T09:00:00Z", "input": "diagnose it",
        "output": "resolved " * 40, "source_slug": "coder", "target_slug": "target",
        "session_id": "session", "notion_task_id": "card", "model_slug": "model",
        "cost_usd": 0.5, "tokens_in": 10, "tokens_out": 20, "error": None,
    }
    row.update(overrides)
    return row


_CFG = {
    "agents_platform_base": "http://ap",
    "kb_base_url": "http://kb",
    "exec_index_url": "http://exec:9408",
    "execution_index_secret": "shared",
    "execution_index_mode": "interesting",
}


class _Recorder:
    """MockTransport handler: answers AP-MT, records every index POST."""

    def __init__(self, responses=None):
        self.posts = []
        self.responses = responses or {}

    def __call__(self, request):
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[
                {"kind": "tool_call", "payload": {"name": "shell"}},
                {"kind": "tool_result", "payload": {"name": "shell", "content": "ok"}},
            ])
        if request.method == "GET":
            return httpx.Response(200, json=_run())
        self.posts.append({
            "host": request.url.host, "path": request.url.path,
            "headers": dict(request.headers),
            "body": json.loads(request.content),
        })
        return self.responses.get(request.url.host) or httpx.Response(200, json={"ok": True})


def _counters(monkeypatch):
    seen = []
    monkeypatch.setattr(idx, "_incr", seen.append)
    return seen


def test_one_run_is_posted_to_both_homes_with_the_identical_payload(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder()
    assert idx.index_run("run-1", config=_CFG,
                         transport=httpx.MockTransport(rec)) is True

    assert [p["host"] for p in rec.posts] == ["kb", "exec"]
    assert [p["path"] for p in rec.posts] == ["/api/kb/executions", "/executions"]
    # Byte-identical payload, not two separately-built ones: the chunks are
    # embedded independently at each destination, so a divergence here would
    # produce two stores that are not comparable at all.
    assert rec.posts[0]["body"] == rec.posts[1]["body"]
    # The two homes read the shared secret from differently-named headers.
    assert rec.posts[0]["headers"]["x-kb-exec-secret"] == "shared"
    assert rec.posts[1]["headers"]["x-exec-index-secret"] == "shared"
    assert "indexed:kb" in seen and "indexed:exec" in seen
    assert "indexed" in seen


def test_the_new_home_failing_does_not_cost_the_old_one_its_write(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder({"exec": httpx.Response(500, text="boom")})
    # Still True: the run reached an index, which is what `indexed` means.
    assert idx.index_run("run-1", config=_CFG,
                         transport=httpx.MockTransport(rec)) is True
    assert [p["host"] for p in rec.posts] == ["kb", "exec"]
    assert "indexed:kb" in seen
    assert "indexed:exec" not in seen
    # Phase-named, so a broken new home is never confused with a broken old
    # one — they have different remedies and both are live during the cutover.
    assert "skipped:exec_post:http_error" in seen
    assert "skipped:http_error" not in seen


def test_the_old_home_failing_does_not_cost_the_new_one_its_write(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder({"kb": httpx.Response(503, text="down")})
    assert idx.index_run("run-1", config=_CFG,
                         transport=httpx.MockTransport(rec)) is True
    # The old target's failure must not short-circuit the loop before the
    # new one is even attempted — the pre-dual-write code returned here.
    assert [p["host"] for p in rec.posts] == ["kb", "exec"]
    assert "indexed:exec" in seen
    assert "skipped:http_error" in seen  # kb keeps its original counter name


def test_a_rejected_secret_is_named_per_target(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder({"exec": httpx.Response(401, text="unauthorized")})
    idx.index_run("run-1", config=_CFG, transport=httpx.MockTransport(rec))
    assert "skipped:exec_post:secret_reject" in seen
    assert "skipped:secret_reject" not in seen


def test_both_homes_failing_reports_the_run_as_not_indexed(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder({"kb": httpx.Response(500), "exec": httpx.Response(500)})
    assert idx.index_run("run-1", config=_CFG,
                         transport=httpx.MockTransport(rec)) is False
    assert "indexed" not in seen


def test_a_blank_target_url_switches_that_home_off_with_no_deploy():
    # Cutover step 6 (stop writing to kb) and its rollback are both this.
    rec = _Recorder()
    assert idx.index_run("run-1", config={**_CFG, "kb_base_url": ""},
                         transport=httpx.MockTransport(rec)) is True
    assert [p["host"] for p in rec.posts] == ["exec"]

    rec = _Recorder()
    assert idx.index_run("run-1", config={**_CFG, "exec_index_url": ""},
                         transport=httpx.MockTransport(rec)) is True
    assert [p["host"] for p in rec.posts] == ["kb"]


def test_both_targets_blank_is_a_named_skip_not_a_silent_success(monkeypatch):
    seen = _counters(monkeypatch)
    rec = _Recorder()
    assert idx.index_run("run-1", config={**_CFG, "kb_base_url": "",
                                          "exec_index_url": ""},
                         transport=httpx.MockTransport(rec)) is False
    assert rec.posts == []
    assert "skipped:no_target" in seen


def test_a_config_predating_this_version_still_gains_the_new_target():
    # An install whose config row was written before exec_index_url existed
    # has no such key at all. Absent must mean "the default", not "off", or
    # the migration would silently never start writing to the new home.
    legacy = {k: v for k, v in _CFG.items() if k != "exec_index_url"}
    targets = idx.exec_index_targets(legacy)
    assert [t.url for t in targets] == [
        "http://kb/api/kb/executions",
        f"{idx.EXEC_INDEX_DEFAULT_URL}/executions",
    ]


def test_the_doctor_discovers_the_new_targets_dynamic_counters():
    # routes.py's failure ratio is a PREFIX rule over `skipped:*`, and
    # counters_snapshot finds dynamically-named buckets by scanning these
    # prefixes. A new phase whose prefix is missing here is invisible to both
    # /execution-index/status and the doctor (design §6, "Counters").
    assert f"skipped:{idx._PHASE_EXEC_POST}:" in idx._DYNAMIC_SKIPPED_PREFIXES
    # ...and the per-target success counters must NOT look like failures.
    assert not any(n.startswith("skipped:") for n in ("indexed:kb", "indexed:exec"))
    assert "indexed:kb" in idx.COUNTER_NAMES
    assert "indexed:exec" in idx.COUNTER_NAMES


def test_the_warm_watcher_path_reaches_the_same_dual_write(monkeypatch):
    # The whole reason the dual-write lives inside index_run: this path never
    # touches the POST code itself, so a call-site implementation would leave
    # it single-target. Asserting on the shape rather than spinning a thread.
    calls = []
    monkeypatch.setattr(idx, "index_run", lambda rid, **kw: calls.append(rid) or True)
    idx._index_and_maybe_queue_retry("run-9", config=_CFG)
    assert calls == ["run-9"]
