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
)

_COUNTER_PREFIX = "execution_index:counters:"

# Kanban observability:execution-index-skipped-other-unclassified: the
# generic except-handler in index_run used to collapse every exception type
# into one anonymous "skipped:other" bucket. It now names the bucket after
# the exception type (e.g. "skipped:other:ReadTimeout") so a NEW failure
# mode shows up named instead of hiding inside a catch-all that already
# swallowed 75% of traffic once. COUNTER_NAMES stays a fixed tuple for the
# well-known buckets — these dynamic names aren't known ahead of time, so
# counters_snapshot() discovers them with a key-pattern scan instead.
_SKIPPED_OTHER_PREFIX = "skipped:other:"

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


def _incr(name: str) -> None:
    """Best-effort INCR — a counting failure must never affect indexing."""
    try:
        client = _counter_redis_client()
        if client is not None:
            client.incr(_COUNTER_PREFIX + name)
    except Exception:
        log.debug("execution index: could not increment counter %s", name, exc_info=True)


def _dynamic_skipped_other_names(client) -> list[str]:
    """Discover ``skipped:other:<ExceptionType>`` names already written to
    Redis — COUNTER_NAMES can't list these ahead of time since the whole
    point is that a new exception type gets its own name the first time it
    occurs. Best-effort: a fake/stub client without ``scan_iter`` (as in
    most of this module's existing tests) just yields none, same as a scan
    that finds nothing."""
    scan_iter = getattr(client, "scan_iter", None)
    if scan_iter is None:
        return []
    try:
        match = _COUNTER_PREFIX + _SKIPPED_OTHER_PREFIX + "*"
        return sorted(key[len(_COUNTER_PREFIX):] for key in scan_iter(match=match))
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
              monotonic: Callable[[], float] = time.monotonic) -> bool:
    """Fetch a finalized run and index it. Never raises to its caller."""
    cfg = _config if config is None else config
    mode = str(cfg.get("execution_index_mode") or "interesting")
    if mode == "off":
        return False
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
                response = client.get(f"{base}/api/runs/{run_id}", timeout=AP_MT_GET_TIMEOUT)
                response.raise_for_status()
                run = response.json()
                if run.get("ended_at") is not None:
                    break
                if monotonic() - started >= deadline_s:
                    log.warning("execution index: run=%s did not finalize before deadline; skipped", run_id)
                    _incr("skipped:finalize_timeout")
                    return False
                sleep(delay)
                delay = min(delay * 2, 5.0)

            response = client.get(f"{base}/api/runs/{run_id}/events",
                                  params={"limit": str(EVENTS_LIMIT)},
                                  timeout=AP_MT_GET_TIMEOUT)
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("run events response is not a list")
            if not _interesting(run, events, mode):
                _incr("skipped:gate")
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
                    _incr("skipped:secret_reject")
                else:
                    log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
                    _incr("skipped:http_error")
                return False
            log.info("execution index: indexed run=%s chunks=%d", run_id, len(chunks))
            _incr("indexed")
            return True
    except httpx.HTTPStatusError as exc:
        # The AP-MT run/events GET calls above failing non-2xx — same bucket
        # as a KB-side error that isn't specifically a secret mismatch.
        log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
        _incr("skipped:http_error")
        return False
    except Exception as exc:  # fail-open: indexing can never affect run delivery
        log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
        _incr(f"{_SKIPPED_OTHER_PREFIX}{type(exc).__name__}")
        return False


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
                            index_run(run_id)
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
