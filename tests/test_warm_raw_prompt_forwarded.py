"""`raw_prompt` has to survive the whole way from the job body to the FIFO.

`warm_pool.dispatch_turn` knows not to reframe a raw turn
(test_dispatch_turn_claude_context.py). This file covers the hop before it:
`_dispatch_warm_turn` reading the flag off the job dict that
agents-platform-multitenant POSTs to /execute. Without that hop the fix is
present and dormant, which is exactly how the bug survived its first fix —
agents-platform has its OWN CliLLM warm path with the identical prepend, that
path was fixed on 2026-09-11, and a live probe afterwards still came back
unfixed because this Runner is the one actually on the path.

Run: python3 -m pytest tests/test_warm_raw_prompt_forwarded.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute, warm_pool  # noqa: E402


def _job(**over) -> dict:
    job = {
        "run_id": "run-1", "agent_id": "agent-1", "session_id": "sess-1",
        "cli": "claude", "prompt": "/compact",
        "notion_task_id": "task-1", "source_device": "telegram",
    }
    job.update(over)
    return job


@pytest.fixture
def dispatched(monkeypatch):
    """Run `_dispatch_warm_turn` with everything around the flag stubbed, and
    hand back the kwargs `dispatch_turn` was actually called with."""
    seen: dict = {}

    monkeypatch.setattr(warm_pool, "get_generation", lambda _url: "epoch-1")
    monkeypatch.setattr(warm_pool, "get_or_create", lambda **_kw: "aw-warm-sess-1")
    monkeypatch.setattr(warm_pool, "maybe_reap", lambda _client: None)
    monkeypatch.setattr(warm_pool, "dispatch_turn",
                        lambda **kw: seen.update(kw))

    def _run(job: dict):
        client = MagicMock()
        execute._dispatch_warm_turn(client, job, "redis://stub")
        return seen

    return _run


def test_a_raw_job_reaches_dispatch_turn_as_raw(dispatched):
    seen = dispatched(_job(raw_prompt=True))

    assert seen["raw_prompt"] is True
    assert seen["prompt"] == "/compact"


def test_an_ordinary_job_is_not_raw(dispatched):
    seen = dispatched(_job(raw_prompt=False, prompt="olá"))

    assert seen["raw_prompt"] is False


def test_a_job_from_an_older_caller_defaults_to_not_raw(dispatched):
    """agents-platform only started sending this key on 2026-09-11. An older
    one omits it, and omitted must keep meaning "ordinary turn" — the header
    is still the only way a warm claude turn learns its own NOTION_TASK_ID."""
    job = _job()
    assert "raw_prompt" not in job, "precondition: the key really is absent"

    assert dispatched(job)["raw_prompt"] is False
