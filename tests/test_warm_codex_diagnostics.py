"""Regression test for the codex-resume diagnostics added 2026-09-04
(Kanban bug:codex-warm-container-rollout-lost-on-resume).

A `thread/resume failed: no rollout found for thread id ... (code -32600)`
turned out to have an intact rollout file AND an intact state_5.sqlite row —
neither the "unpinned AW_AGENT_TAG image drift" nor the "concurrent codex
writers on the shared $CODEX_HOME" hypothesis could be checked after the
fact, because nothing recorded which image tag a warm codex container was
actually spawned from. This pins that the tag now reaches the container's
own environment, where aw-warm-relay-codex.py's `_diag()` logs it next to
`codex --version`'s resolved output on every resume attempt.

Run: .venv/aw/bin/python -m pytest tests/test_warm_codex_diagnostics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

BASE_CODEX_JOB = {
    "run_id": "r1",
    "cli": "codex",
    "agent_id": "agent-1",
    "session_id": "thread-1",
    "prompt": "hi",
}


def test_warm_codex_container_gets_agent_tag_env(tmp_path, monkeypatch):
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "DEFAULT_TAG", "sha-abc123")

    _image, kwargs = execute_mod._build_warm_kwargs_codex(
        dict(BASE_CODEX_JOB), "epoch1", "redis://example.test:6379/0")

    # The floating/unpinned tag this container was actually spawned from —
    # aw-warm-relay-codex.py logs it alongside `codex --version` so a future
    # investigation can tell image drift apart from a same-version cause.
    assert kwargs["environment"]["AW_AGENT_TAG"] == "sha-abc123"
