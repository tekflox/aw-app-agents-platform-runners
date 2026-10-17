"""Best-effort post-run indexing into aw-app-kb's execution index."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

import httpx

from . import platform_base as platform_base_mod
from . import shared_redis

log = logging.getLogger("aw_apps.agents_platform_runners.execution_index")

EVENTS_LIMIT = 5000
MAX_CHARS = 1500
MAX_CHUNKS = 12
TERMINAL = {"success", "error", "cancelled"}

# The shared httpx.Client below is built with timeout=5. aw-app-kb's POST
# /api/kb/executions does not meet that: it runs synchronous embedding
# generation (exec_pg.py's _kb._embed_docs) before the INSERT, measured live
# at 18.07s for a SINGLE trivial chunk, and real payloads carry up to
# MAX_CHUNKS=12. That mismatch was the entire cause of Kanban
# observability:execution-index-skipped-other-unclassified: every one of
# those POSTs raised httpx.ReadTimeout, which isn't an httpx.HTTPStatusError
# so it fell through both specific handlers into the generic catch-all. This
# is a per-call override (httpx.Client.post(..., timeout=...)), not a change
# to the shared client's default — the shared 5s stays the floor.
KB_POST_TIMEOUT_S = 120.0

# The two AP-MT GETs (run poll, events) used to inherit the shared client's
# flat timeout=5 too — but a flat scalar applies the same budget to connect
# AND read. Connect is where "AP-MT is unreachable" must fail fast, so that
# stays 5s (resilience:execution-index-apmt-get-timeout-5s, Kanban
# 3fc5bf3b-9510-8158-800a-ea32a7be662e): index_run only ever runs on a daemon
# background thread (start()/start_after_stream_done below), so no GET
# budget can delay run delivery — the 5s connect fast-fail is about bailing
# out of a dead AP-MT quickly, not about the delivery path. Read is a
# different failure mode: AP-MT alive but slow to answer under load, which
# is exactly the state measured live on 2026-10-17 (skipped:other:ReadTimeout
# 0→4 while a sequential backfill hammered /api/runs) — and load is when the
# most runs finish and the most needs indexing, so a 5s read budget goes
# blind during its busiest windows. This per-call override raises only the
# read phase; if the ReadTimeout counter climbs again with read=30, fix
# AP-MT's slow tail server-side rather than bumping this further.
AP_MT_GET_TIMEOUT = httpx.Timeout(5.0, read=30.0)

_config: dict = {}

# Redis-backed counters (Kanban bug:execution-index-hook-silently-drops-most-
# runs) — a plain in-process dict would undercount silently the moment this
# app runs more than one worker, same class of bug as every other WORKERS=N
# per-process-state gap already in this workspace's KB. Fixed, named reasons
# instead of one "skipped" bucket: the whole point is telling "the KB
# rejected our secret" apart from "the gate correctly excluded this run".
#
# warm_watchers_started/resolved are the direct fingerprint of the dominant
# root cause: start_after_stream_done's watcher thread is killed silently by
# any restart of this (Tier-1, in-process) app before it ever reaches a
# resolution branch — a persistent, growing gap between the two is proof of
# exactly that, and nothing else in this system can show it.
COUNTER_NAMES = (
    "attempted",
    "indexed",
    "skipped:gate",
    "skipped:finalize_timeout",
    "skipped:http_error",
    "skipped:secret_reject",
    "skipped:other",
    "warm_watchers_started",
    "warm_watchers_resolved",
    "retry_exhausted",
)

_COUNTER_PREFIX = "execution_index:counters:"

# Windowed mirror of the lifetime counters above (Kanban observability:
# execution-index-doctor-false-alarm-from-gate-exclusions): the doctor
# check's failure ratio must forget an old incident instead of staying red
# forever, so each counter also gets an hourly UTC bucket key alongside its
# lifetime key. WINDOW_LOOKBACK_HOURS is the nominal span the doctor reads;
# the snapshot actually sums WINDOW_LOOKBACK_HOURS+1 buckets (the current,
# still-filling hour plus that many full hours back) so the lookback never
# undercounts a window that started mid-hour. TTL outlives the lookback by
# a comfortable margin (a bucket is only ever written during its own hour,
# so it is read for at most ~25h after creation) — the keyspace is self-
# pruning, no reset job, no manual surgery.
WINDOW_LOOKBACK_HOURS = 24
_WINDOW_BUCKET_COUNT = WINDOW_LOOKBACK_HOURS + 1
_WINDOW_BUCKET_TTL_S = 26 * 3600
_WINDOW_BUCKET_PREFIX = _COUNTER_PREFIX + "w:"

# Kanban observability:execution-index-skipped-other-unclassified: the
# generic except-handler in index_run used to collapse every exception type
# into one anonymous "skipped:other" bucket. It now names the bucket after
# the exception type (e.g. "skipped:other:ReadTimeout") so a NEW failure
# mode shows up named instead of hiding inside a catch-all that already
# swallowed 75% of traffic once. COUNTER_NAMES stays a fixed tuple for the
# well-known buckets — these dynamic names aren't known ahead of time, so
# counters_snapshot() discovers them with a key-pattern scan instead.
_SKIPPED_OTHER_PREFIX = "skipped:other:"

# Kanban observability:execution-index-readtimeout-has-no-call-site:
# skipped:other:<ExceptionType> named the exception but not which of
# index_run's three HTTP calls raised it, so a ReadTimeout from the fast
# AP-MT run-poll, the AP-MT events GET, or the slow KB POST were all
# indistinguishable even though each has a different remedy. index_run
# attributes an exception from any of these three call sites to its own
# phase-named bucket instead; an exception raised outside all three (e.g.
# a bad /events payload) still falls into the generic _SKIPPED_OTHER_PREFIX
# bucket above.
_PHASE_AP_MT_GET_RUN = "apmt_get_run"
_PHASE_AP_MT_GET_EVENTS = "apmt_get_events"
_PHASE_KB_POST = "kb_post"

# Every prefix counters_snapshot()/counters_window_snapshot() must scan for
# dynamically-named skipped:<...>:<ExceptionType> keys — the generic
# catch-all plus the three phase-attributed buckets above.
_DYNAMIC_SKIPPED_PREFIXES = (
    _SKIPPED_OTHER_PREFIX,
    f"skipped:{_PHASE_AP_MT_GET_RUN}:",
    f"skipped:{_PHASE_AP_MT_GET_EVENTS}:",
    f"skipped:{_PHASE_KB_POST}:",
)

# Durable retry queue (card 3fc5bf3b-9510-81ec-8752-fc06d82a8a5e): live
# traffic loses ~19% of eligible runs to a ONE-SHOT warm watcher
# (start_after_stream_done) that just discards any skipped:* outcome,
# while a synchronous backfill re-running the exact same index_run() loses
# only 0.1% — the backfill is just never racing AP-MT's own finalize/read
# timing, so its cleanliness is survivorship, not extra robustness. A
# Redis HASH of run_id -> attempt_count (not a counter-style INCR key) so
# the sweep can both enumerate what's pending and give up on an entry that
# keeps failing instead of sweeping it forever. skipped:gate (the
# _interesting() gate's intentional exclusion) must never be queued or
# kept queued — it isn't a failure, there's nothing to retry.
_RETRY_QUEUE_KEY = "execution_index:retry_queue"
_RETRY_MAX_ATTEMPTS = 24  # ~1 day of hourly sweeps before giving up on an entry

_counter_lock = threading.Lock()
_counter_client = None
_counter_client_url: str | None = None


def _counter_redis_client():
    """Lazily resolve and cache a sync Redis client for the counters.

    Reuses the same shared Redis every run event already publishes to
    (``shared_redis.resolve``) rather than inventing a second one. Returns
    None when no Redis is configured/discoverable — callers must treat that
    as "counting is unavailable", never as a reason to skip indexing itself.
    """
    global _counter_client, _counter_client_url
    url = shared_redis.resolve(_config)
    if not url:
        return None
    with _counter_lock:
        if _counter_client is None or _counter_client_url != url:
            import redis
            _counter_client = redis.Redis.from_url(
                url, decode_responses=True, socket_connect_timeout=5, socket_timeout=30)
            _counter_client_url = url
        return _counter_client


def _hour_bucket(ts: float | None = None) -> str:
    """UTC hour bucket, e.g. ``2026101715`` — a pure function of wall-clock
    time, so every WORKERS=N worker derives the identical key for "now"
    with no coordination between them."""
    return time.strftime("%Y%m%d%H", time.gmtime(ts if ts is not None else time.time()))


def _incr(name: str) -> None:
    """Best-effort INCR — a counting failure must never affect indexing.

    Dual-writes in one pipeline: the existing lifetime key, and an hourly
    UTC bucket key (``_WINDOW_BUCKET_PREFIX<name>:<hour>``) that the
    doctor's windowed failure ratio reads via ``counters_window_snapshot``.
    Both writes stay inside this one try — a bucket-write failure must
    never affect indexing or the lifetime count, same as before."""
    try:
        client = _counter_redis_client()
        if client is not None:
            bucket_key = _WINDOW_BUCKET_PREFIX + name + ":" + _hour_bucket()
            pipe = client.pipeline()
            pipe.incr(_COUNTER_PREFIX + name)
            pipe.incr(bucket_key)
            pipe.expire(bucket_key, _WINDOW_BUCKET_TTL_S)
            pipe.execute()
    except Exception:
        log.debug("execution index: could not increment counter %s", name, exc_info=True)


def _queue_retry(run_id: str, reason: str) -> None:
    """Best-effort: queue ``run_id`` for the hourly retry sweep, recording
    the exact ``skipped:<reason>`` that sent it here as ``<attempts>:
    <reason>`` — a QA-accountability condition on this card's own fix: a
    self-healing retry that drains silently trades a *visible* 19% loss
    for an *invisible* one, which is worse. ``HSETNX`` so a run already
    queued (e.g. the sweep itself re-queuing it) keeps its ORIGINAL attempt
    count and ORIGINAL reason — a later, possibly different failure on
    retry doesn't overwrite why it first landed here. Same fail-open
    contract as ``_incr`` — a queuing failure must never affect indexing
    itself."""
    try:
        client = _counter_redis_client()
        if client is not None:
            client.hsetnx(_RETRY_QUEUE_KEY, run_id, f"0:{reason}")
    except Exception:
        log.debug("execution index: could not queue retry for run=%s", run_id, exc_info=True)


def _dynamic_skipped_other_names(client) -> list[str]:
    """Discover dynamically-named ``skipped:<...>:<ExceptionType>`` keys
    already written to Redis — one scan per prefix in
    ``_DYNAMIC_SKIPPED_PREFIXES``, since COUNTER_NAMES can't list these
    ahead of time: the whole point is that a new (call-site, exception
    type) pair gets its own name the first time it occurs. Best-effort: a
    fake/stub client without ``scan_iter`` (as in most of this module's
    existing tests) just yields none, same as a scan that finds nothing."""
    scan_iter = getattr(client, "scan_iter", None)
    if scan_iter is None:
        return []
    names: set[str] = set()
    try:
        for prefix in _DYNAMIC_SKIPPED_PREFIXES:
            match = _COUNTER_PREFIX + prefix + "*"
            names.update(key[len(_COUNTER_PREFIX):] for key in scan_iter(match=match))
        return sorted(names)
    except Exception:
        return []


def counters_snapshot() -> dict[str, int | None]:
    """Read-only snapshot of every counter — used by the status endpoint and
    the doctor check. Values are None (rather than 0) when Redis itself is
    unavailable, so a caller can tell "nothing has happened yet" apart from
    "we cannot even measure this right now". Includes whichever dynamic
    skipped:other:<ExceptionType> buckets have been written, on top of the
    fixed COUNTER_NAMES set."""
    try:
        client = _counter_redis_client()
    except Exception:
        client = None
    if client is None:
        return {name: None for name in COUNTER_NAMES}
    names = COUNTER_NAMES + tuple(_dynamic_skipped_other_names(client))
    try:
        raw = client.mget([_COUNTER_PREFIX + name for name in names])
    except Exception:
        return {name: None for name in COUNTER_NAMES}
    return {name: (int(value) if value is not None else 0)
            for name, value in zip(names, raw)}


def _dynamic_skipped_other_names_window(client) -> list[str]:
    """Window counterpart of ``_dynamic_skipped_other_names`` — scans the
    bucket pattern for each prefix in ``_DYNAMIC_SKIPPED_PREFIXES``
    (``...:w:skipped:other:*``, ``...:w:skipped:apmt_get_run:*``, etc.),
    not the lifetime one, and parses the counter name back out by
    stripping the trailing ``:<hour>`` segment. Counter names themselves
    contain colons (``skipped:apmt_get_run:ReadTimeout``), so this splits
    on the LAST colon — safe because the hour suffix is always a plain
    digit string with no colon of its own."""
    scan_iter = getattr(client, "scan_iter", None)
    if scan_iter is None:
        return []
    names: set[str] = set()
    try:
        for prefix in _DYNAMIC_SKIPPED_PREFIXES:
            match = _WINDOW_BUCKET_PREFIX + prefix + "*"
            for key in scan_iter(match=match):
                suffix = key[len(_WINDOW_BUCKET_PREFIX):]
                name, _, _hour = suffix.rpartition(":")
                if name:
                    names.add(name)
        return sorted(names)
    except Exception:
        return []


def _window_bucket_keys(name: str, bucket_count: int = _WINDOW_BUCKET_COUNT) -> list[str]:
    now = time.time()
    return [_WINDOW_BUCKET_PREFIX + name + ":" + _hour_bucket(now - i * 3600)
            for i in range(bucket_count)]


def counters_window_snapshot(hours: int = WINDOW_LOOKBACK_HOURS) -> dict[str, int | None]:
    """Windowed mirror of ``counters_snapshot()`` — the doctor check's
    failure ratio reads this instead of the lifetime counters, so an old
    incident ages out instead of keeping the check red forever. Sums
    ``hours + 1`` hourly UTC buckets (the current, still-filling hour plus
    that many full hours back) per counter name — one MGET per name, same
    shape as the lifetime snapshot's single MGET across all names, just
    repeated because each name has its own set of bucket keys. ``None``
    for every name when Redis is unreachable — same contract as
    ``counters_snapshot()``."""
    try:
        client = _counter_redis_client()
    except Exception:
        client = None
    if client is None:
        return {name: None for name in COUNTER_NAMES}
    bucket_count = hours + 1
    names = COUNTER_NAMES + tuple(_dynamic_skipped_other_names_window(client))
    try:
        result: dict[str, int | None] = {}
        for name in names:
            raw = client.mget(_window_bucket_keys(name, bucket_count))
            result[name] = sum(int(value) for value in raw if value is not None)
        return result
    except Exception:
        return {name: None for name in COUNTER_NAMES}


def configure(config: dict | None) -> None:
    """Keep the live config object so Settings changes need no restart."""
    global _config
    _config = config if config is not None else {}


_REDACTIONS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|gho)_[A-Za-z0-9]{8,}"),
    re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2}"),
    re.compile(r"(?i)(\bX-Api-Key\s*:\s*)[^\s,;\"']+"),
)


def redact(value: str) -> str:
    for pattern in _REDACTIONS:
        value = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", value)
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _split(text: str) -> list[str]:
    text = redact(text)
    return [text[i:i + MAX_CHARS] for i in range(0, len(text), MAX_CHARS)] or [""]


def _event_kind(event: dict) -> str:
    return str(event.get("kind") or event.get("type") or "event")


def _event_text(event: dict) -> str:
    return f"{_event_kind(event)}: {_text(event)}"


def _chunks(run: dict, events: list[dict], events_truncated: bool) -> list[dict]:
    """Build bounded chunks in relevance order: summary, error, tools, trace."""
    candidates: list[tuple[str, str]] = []
    summary = {
        "run_id": run.get("id") or run.get("run_id"),
        "status": run.get("status"),
        "agent_slug": run.get("agent_slug") or run.get("source_slug"),
        "target_slug": run.get("target_slug"),
        "input": str(run.get("input") or "")[:500],
        "output": str(run.get("output") or "")[:500],
    }
    # Keep summary useful without allowing huge I/O to starve tool evidence.
    candidates.append(("summary", _text(summary)[: MAX_CHARS * 2]))
    if run.get("error"):
        candidates.append(("io", f"error: {_text(run['error'])}"))

    tool_events = [e for e in events if _event_kind(e) in {"tool_call", "tool_result"}]
    for i in range(0, len(tool_events), 2):
        candidates.append(("trace", "\n".join(_event_text(e) for e in tool_events[i:i + 2])))
    if run.get("input"):
        candidates.append(("io", f"input: {_text(run['input'])}"))
    if run.get("output"):
        candidates.append(("io", f"output: {_text(run['output'])}"))
    other_events = [e for e in events if _event_kind(e) not in {"tool_call", "tool_result"}]
    if other_events:
        candidates.append(("trace", "\n".join(_event_text(e) for e in other_events)))

    tool_names = sorted({str(e.get("tool_name") or e.get("name") or
                             (e.get("payload") or {}).get("name")) for e in tool_events
                         if e.get("tool_name") or e.get("name") or
                         (e.get("payload") or {}).get("name")})
    common = {
        "run_id": run.get("id") or run.get("run_id"),
        "status": run.get("status"),
        "agent_slug": run.get("agent_slug") or run.get("source_slug"),
        "target_slug": run.get("target_slug"),
        "session_id": run.get("session_id"),
        "notion_task_id": run.get("notion_task_id"),
        "model_slug": run.get("model_slug"),
        "cost_usd": run.get("cost_usd"),
        "tokens_in": run.get("tokens_in"),
        "tokens_out": run.get("tokens_out"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        # Metadata is duplicated on every chunk; cap it so one traceback
        # cannot bypass the 12x1500-character retention budget.
        "error": redact(_text(run.get("error") or ""))[:500],
        "tool_names": tool_names,
        "events_truncated": events_truncated,
    }
    result = []
    for kind, content in candidates:
        for part in _split(content):
            if len(result) >= MAX_CHUNKS:
                return result
            result.append({"seq": len(result), "content": part,
                           "metadata": {**common, "chunk_kind": kind}})
    return result


def _interesting(run: dict, events: list[dict], mode: str) -> bool:
    status = str(run.get("status") or "")
    if status not in TERMINAL:
        return False
    prompt = str(run.get("input") or "").strip()
    initiator = run.get("initiator_kind")
    if prompt == "/compact" or initiator in {"auto_compact", "pending_compact"}:
        return False
    if status == "cancelled" and not run.get("output"):
        return False
    if mode == "failures" and status != "error":
        return False
    if mode == "interesting" and not (run.get("error") or any(_event_kind(e) == "tool_call" for e in events)):
        return False
    if not events and len(str(run.get("output") or "")) < 200:
        return False
    return True


def index_run(run_id: str, *, config: dict | None = None,
              transport: httpx.BaseTransport | None = None,
              deadline_s: float = 60.0, sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic,
              on_skip: Callable[[str], None] | None = None) -> bool:
    """Fetch a finalized run and index it. Never raises to its caller.

    ``on_skip``, if given, is called with the exact ``skipped:<reason>``
    counter name right before it is incremented — lets a caller (the warm
    watcher, the retry sweep) learn WHY a run was skipped without
    index_run growing a second return channel every other caller would
    have to thread through."""
    cfg = _config if config is None else config
    mode = str(cfg.get("execution_index_mode") or "interesting")
    if mode == "off":
        return False

    def _skip(reason: str) -> None:
        _incr(reason)
        if on_skip is not None:
            try:
                on_skip(reason)
            except Exception:
                log.debug("execution index: on_skip callback failed", exc_info=True)

    _incr("attempted")
    base = platform_base_mod.resolve(cfg).rstrip("/")
    kb_base = str(cfg.get("kb_base_url") or "http://aw-app-kb:8000").rstrip("/")
    secret = str(cfg.get("execution_index_secret") or "")
    headers = ({"Authorization": f"Bearer {cfg['agents_platform_token']}"}
               if cfg.get("agents_platform_token") else {})
    started = monotonic()
    delay = 0.25
    try:
        with httpx.Client(headers=headers, timeout=5, transport=transport) as client:
            while True:
                try:
                    response = client.get(f"{base}/api/runs/{run_id}", timeout=AP_MT_GET_TIMEOUT)
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    raise
                except Exception as exc:
                    log.warning("execution index: skipped run=%s after error in %s: %s",
                                run_id, _PHASE_AP_MT_GET_RUN, exc)
                    _skip(f"skipped:{_PHASE_AP_MT_GET_RUN}:{type(exc).__name__}")
                    return False
                run = response.json()
                if run.get("ended_at") is not None:
                    break
                if monotonic() - started >= deadline_s:
                    log.warning("execution index: run=%s did not finalize before deadline; skipped", run_id)
                    _skip("skipped:finalize_timeout")
                    return False
                sleep(delay)
                delay = min(delay * 2, 5.0)

            try:
                response = client.get(f"{base}/api/runs/{run_id}/events",
                                      params={"limit": str(EVENTS_LIMIT)},
                                      timeout=AP_MT_GET_TIMEOUT)
                response.raise_for_status()
            except httpx.HTTPStatusError:
                raise
            except Exception as exc:
                log.warning("execution index: skipped run=%s after error in %s: %s",
                            run_id, _PHASE_AP_MT_GET_EVENTS, exc)
                _skip(f"skipped:{_PHASE_AP_MT_GET_EVENTS}:{type(exc).__name__}")
                return False
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("run events response is not a list")
            if not _interesting(run, events, mode):
                _skip("skipped:gate")
                return False
            truncated = len(events) >= EVENTS_LIMIT
            chunks = _chunks(run, events, truncated)
            payload = {
                "run_id": run_id,
                "chunks": chunks,
                "metadata_common": {"events_truncated": truncated},
            }
            try:
                response = client.post(
                    f"{kb_base}/api/kb/executions", json=payload,
                    headers={"X-KB-Exec-Secret": secret},
                    timeout=KB_POST_TIMEOUT_S,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # A wrong/rotated execution_index_secret answers 401/403 —
                # distinguish it from a generic KB-side error so this counter
                # alone can name a credential mismatch without a log grep.
                if exc.response.status_code in (401, 403):
                    log.warning("execution index: skipped run=%s — kb rejected secret (%s)",
                                run_id, exc.response.status_code)
                    _skip("skipped:secret_reject")
                else:
                    log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
                    _skip("skipped:http_error")
                return False
            except Exception as exc:
                log.warning("execution index: skipped run=%s after error in %s: %s",
                            run_id, _PHASE_KB_POST, exc)
                _skip(f"skipped:{_PHASE_KB_POST}:{type(exc).__name__}")
                return False
            log.info("execution index: indexed run=%s chunks=%d", run_id, len(chunks))
            _incr("indexed")
            return True
    except httpx.HTTPStatusError as exc:
        # The AP-MT run/events GET calls above failing non-2xx — same bucket
        # as a KB-side error that isn't specifically a secret mismatch.
        log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
        _skip("skipped:http_error")
        return False
    except Exception as exc:  # fail-open: indexing can never affect run delivery
        log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
        _skip(f"{_SKIPPED_OTHER_PREFIX}{type(exc).__name__}")
        return False


def _index_and_maybe_queue_retry(run_id: str, *, config: dict | None = None,
                                 transport: httpx.BaseTransport | None = None) -> bool:
    """``index_run()`` once; any genuine failure — anything but success or
    the legitimate ``skipped:gate`` exclusion — gets queued for the hourly
    retry sweep instead of being lost outright. Shared by the warm watcher
    below and (indirectly, via re-queuing) by the sweep itself. ``transport``
    is a test-only seam (production always lets it default to ``None``,
    the real network)."""
    skip_reason: list[str] = []
    ok = index_run(run_id, config=config, transport=transport, on_skip=skip_reason.append)
    if not ok and skip_reason and skip_reason[0] != "skipped:gate":
        _queue_retry(run_id, skip_reason[0])
    return ok


def sweep_retry_queue(config: dict | None = None) -> dict[str, int]:
    """Drain the retry queue: synchronously re-run ``index_run()`` on each
    queued run_id — the exact pattern ``scripts/backfill_execution_index.py``
    already proved safe (0.1% loss over 1013 runs), reused here rather than
    reinvented, because by the time this sweep runs (hourly) every queued
    run has long since finalized — a plain retry, not a race against
    anything. Removes an entry on success or on a legitimate
    ``skipped:gate`` exclusion (not a failure); any other skip leaves it
    queued for the next sweep, up to ``_RETRY_MAX_ATTEMPTS`` before giving
    up (counted in ``retry_exhausted`` so a permanently stuck entry is
    visible instead of silently disappearing)."""
    result = {"queued": 0, "indexed": 0, "gated": 0, "requeued": 0, "exhausted": 0}
    try:
        client = _counter_redis_client()
    except Exception:
        client = None
    if client is None:
        return result
    try:
        entries = client.hgetall(_RETRY_QUEUE_KEY)
    except Exception:
        log.warning("execution index: retry sweep could not read the queue", exc_info=True)
        return result
    result["queued"] = len(entries)
    for run_id, value in entries.items():
        attempts_raw, _, original_reason = str(value).partition(":")
        skip_reason: list[str] = []
        ok = index_run(run_id, config=config, on_skip=skip_reason.append)
        if ok:
            result["indexed"] += 1
            client.hdel(_RETRY_QUEUE_KEY, run_id)
        elif skip_reason and skip_reason[0] == "skipped:gate":
            result["gated"] += 1
            client.hdel(_RETRY_QUEUE_KEY, run_id)
        else:
            attempts = int(attempts_raw or 0) + 1
            if attempts >= _RETRY_MAX_ATTEMPTS:
                result["exhausted"] += 1
                client.hdel(_RETRY_QUEUE_KEY, run_id)
                _incr("retry_exhausted")
                log.warning("execution index: retry exhausted run=%s original_reason=%s "
                            "last_reason=%s", run_id, original_reason,
                            skip_reason[0] if skip_reason else None)
            else:
                result["requeued"] += 1
                client.hset(_RETRY_QUEUE_KEY, run_id, f"{attempts}:{original_reason}")
    if result["queued"]:
        log.info("execution index: retry sweep %s", result)
    return result


def retry_queue_snapshot() -> dict[str, dict[str, Any]] | None:
    """Read-only view of the retry queue for ``/execution-index/status`` —
    the QA-accountability condition on card 3fc5bf3b-9510-81ec-8752-
    fc06d82a8a5e: a run stuck in the queue, and the exact reason it landed
    there, must be visible without a redis-cli session, or this fix trades
    a visible 19% loss for an invisible one. ``None`` when Redis itself is
    unavailable — same contract as ``counters_snapshot``."""
    try:
        client = _counter_redis_client()
    except Exception:
        client = None
    if client is None:
        return None
    try:
        entries = client.hgetall(_RETRY_QUEUE_KEY)
    except Exception:
        return None
    result: dict[str, dict[str, Any]] = {}
    for run_id, value in entries.items():
        attempts_raw, _, reason = str(value).partition(":")
        result[run_id] = {"attempts": int(attempts_raw or 0), "reason": reason}
    return result


def start(run_id: str) -> threading.Thread | None:
    if str(_config.get("execution_index_mode") or "interesting") == "off":
        return None
    try:
        thread = threading.Thread(target=index_run, args=(run_id,),
                                  name=f"execution-index-{run_id[:12]}", daemon=True)
        thread.start()
        return thread
    except Exception as exc:
        log.warning("execution index: could not start run=%s: %s", run_id, exc)
        return None


def start_after_stream_done(run_id: str, redis_url: str) -> threading.Thread | None:
    """Warm runs publish ``done`` inside their relay; wait without consuming it."""
    if str(_config.get("execution_index_mode") or "interesting") == "off":
        return None

    def _wait() -> None:
        client = None
        try:
            import redis
            # Same connect/read timeouts as execute.py's own _redis_client()
            # helper — this client used to set neither, leaving it able to
            # hang indefinitely on a stale/half-open socket below the
            # xread(block=5000) bound, with no exception and no warning.
            client = redis.Redis.from_url(redis_url, decode_responses=True,
                                          socket_connect_timeout=5, socket_timeout=30)
            cursor = "0-0"
            deadline = time.monotonic() + 24 * 3600
            key = f"run:{run_id}:events"
            while time.monotonic() < deadline:
                batches = client.xread({key: cursor}, count=100, block=5000)
                for _stream, entries in batches:
                    for entry_id, fields in entries:
                        cursor = entry_id
                        if str(fields.get("done") or "").lower() in {"1", "true"}:
                            _index_and_maybe_queue_retry(run_id)
                            _incr("warm_watchers_resolved")
                            return
            log.warning("execution index: warm run=%s emitted no done before watcher deadline", run_id)
            _incr("warm_watchers_resolved")
        except Exception as exc:
            log.warning("execution index: warm completion watch failed run=%s: %s", run_id, exc)
            _incr("warm_watchers_resolved")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    try:
        thread = threading.Thread(target=_wait, name=f"execution-index-wait-{run_id[:12]}",
                                  daemon=True)
        # Right before thread.start(): a persistent, growing gap between this
        # and warm_watchers_resolved is the direct fingerprint of a watcher
        # thread killed by a restart of this (Tier-1, in-process) app before
        # it ever reached one of _wait()'s three resolution branches above —
        # see this module's module-level COUNTER_NAMES comment.
        _incr("warm_watchers_started")
        thread.start()
        return thread
    except Exception as exc:
        log.warning("execution index: could not watch warm run=%s: %s", run_id, exc)
        return None
