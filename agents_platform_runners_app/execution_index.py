"""Best-effort post-run indexing into aw-app-kb's execution index."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.execution_index")

EVENTS_LIMIT = 5000
MAX_CHARS = 1500
MAX_CHUNKS = 12
TERMINAL = {"success", "error", "cancelled"}

_config: dict = {}


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
    base = str(cfg.get("agents_platform_base") or "http://172.18.0.1:10014").rstrip("/")
    kb_base = str(cfg.get("kb_base_url") or "http://aw-app-kb:8000").rstrip("/")
    secret = str(cfg.get("execution_index_secret") or "")
    headers = ({"Authorization": f"Bearer {cfg['agents_platform_token']}"}
               if cfg.get("agents_platform_token") else {})
    started = monotonic()
    delay = 0.25
    try:
        with httpx.Client(headers=headers, timeout=5, transport=transport) as client:
            while True:
                response = client.get(f"{base}/api/runs/{run_id}")
                response.raise_for_status()
                run = response.json()
                if run.get("ended_at") is not None:
                    break
                if monotonic() - started >= deadline_s:
                    log.warning("execution index: run=%s did not finalize before deadline; skipped", run_id)
                    return False
                sleep(delay)
                delay = min(delay * 2, 5.0)

            response = client.get(f"{base}/api/runs/{run_id}/events",
                                  params={"limit": str(EVENTS_LIMIT)})
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("run events response is not a list")
            if not _interesting(run, events, mode):
                return False
            truncated = len(events) >= EVENTS_LIMIT
            chunks = _chunks(run, events, truncated)
            payload = {
                "run_id": run_id,
                "chunks": chunks,
                "metadata_common": {"events_truncated": truncated},
            }
            response = client.post(
                f"{kb_base}/api/kb/executions", json=payload,
                headers={"X-KB-Exec-Secret": secret},
            )
            response.raise_for_status()
            log.info("execution index: indexed run=%s chunks=%d", run_id, len(chunks))
            return True
    except Exception as exc:  # fail-open: indexing can never affect run delivery
        log.warning("execution index: skipped run=%s after error: %s", run_id, exc)
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
            client = redis.Redis.from_url(redis_url, decode_responses=True)
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
                            return
            log.warning("execution index: warm run=%s emitted no done before watcher deadline", run_id)
        except Exception as exc:
            log.warning("execution index: warm completion watch failed run=%s: %s", run_id, exc)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    try:
        thread = threading.Thread(target=_wait, name=f"execution-index-wait-{run_id[:12]}",
                                  daemon=True)
        thread.start()
        return thread
    except Exception as exc:
        log.warning("execution index: could not watch warm run=%s: %s", run_id, exc)
        return None
