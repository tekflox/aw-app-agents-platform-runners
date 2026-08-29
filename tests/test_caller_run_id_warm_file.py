"""mcp_server._caller_run_id() (commit f0d1bef, 2026-08-29).

In warm mode this MCP server is a single stdio subprocess kept alive for a
container's whole 6h TTL — its own os.environ and any header baked into its
mcp.json are fixed at turn 1 and never refresh. Before this fix,
_caller_run_id() read _gateway_caller_run_id (from the X-Aw-Caller-Run-Id
header) or AW_RUN_ID as a fallback — both permanently pinned to turn 1's run,
which silently broke every per-run gate keyed on it (schedule_wakeup's
dedup-by-origin_run_id chief among them: once one wakeup fired, every later
one in the same warm container was refused as "already armed").

warm_pool.dispatch_turn() already writes _WARM_CURRENT_RUN_ID_PATH fresh at
the START of every turn. _caller_run_id() now reads that file FIRST, only
falling back to the header/env pair (correct for the ephemeral, non-warm,
per-run-container path where the whole process really is fresh every turn).

Run: .venv/aw/bin/python -m pytest tests/test_caller_run_id_warm_file.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import mcp_server  # noqa: E402


def _point_warm_file_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_WARM_CURRENT_RUN_ID_PATH", str(path))


def test_warm_file_present_and_nonempty_wins_over_args_and_env(tmp_path, monkeypatch):
    """The primary fix: when the per-turn warm file exists, its content is
    authoritative — args and env are never even consulted."""
    warm_file = tmp_path / "current_run_id"
    warm_file.write_text("run-from-warm-file\n", encoding="utf-8")
    _point_warm_file_at(monkeypatch, warm_file)
    monkeypatch.setenv("AW_RUN_ID", "run-from-stale-env")

    result = mcp_server._caller_run_id({"_gateway_caller_run_id": "run-from-stale-header"})

    assert result == "run-from-warm-file"


def test_warm_file_absent_falls_back_to_gateway_caller_run_id_arg(tmp_path, monkeypatch):
    """No warm file (ephemeral / non-warm container path) — args'
    _gateway_caller_run_id (the X-Aw-Caller-Run-Id header, gateway-injected)
    is the next source."""
    missing_file = tmp_path / "does-not-exist" / "current_run_id"
    _point_warm_file_at(monkeypatch, missing_file)
    monkeypatch.setenv("AW_RUN_ID", "run-from-env")

    result = mcp_server._caller_run_id({"_gateway_caller_run_id": "run-from-header"})

    assert result == "run-from-header"


def test_warm_file_absent_and_no_gateway_arg_falls_back_to_env(tmp_path, monkeypatch):
    """No warm file, no gateway-injected arg (e.g. a direct/local stdio
    invocation) — AW_RUN_ID is the last resort."""
    missing_file = tmp_path / "does-not-exist" / "current_run_id"
    _point_warm_file_at(monkeypatch, missing_file)
    monkeypatch.setenv("AW_RUN_ID", "run-from-env")

    result = mcp_server._caller_run_id({})

    assert result == "run-from-env"


def test_no_source_available_returns_none(tmp_path, monkeypatch):
    """No warm file, no gateway arg, no env var — nothing to resolve, and
    that must surface as None rather than raising or returning a stale
    empty-string sentinel."""
    missing_file = tmp_path / "does-not-exist" / "current_run_id"
    _point_warm_file_at(monkeypatch, missing_file)
    monkeypatch.delenv("AW_RUN_ID", raising=False)

    result = mcp_server._caller_run_id({})

    assert result is None


def test_empty_warm_file_falls_through_to_the_next_source(tmp_path, monkeypatch):
    """A warm file that exists but is empty (e.g. truncated mid-write, or
    read in the narrow window before dispatch_turn's first write lands) must
    not shadow a real fallback with an empty string."""
    warm_file = tmp_path / "current_run_id"
    warm_file.write_text("", encoding="utf-8")
    _point_warm_file_at(monkeypatch, warm_file)
    monkeypatch.setenv("AW_RUN_ID", "run-from-env")

    result = mcp_server._caller_run_id({})

    assert result == "run-from-env"


def test_warm_file_content_is_stripped(tmp_path, monkeypatch):
    """dispatch_turn() writes the run id possibly with trailing whitespace/
    newline — pin that _caller_run_id returns the clean value."""
    warm_file = tmp_path / "current_run_id"
    warm_file.write_text("  run-with-whitespace  \n", encoding="utf-8")
    _point_warm_file_at(monkeypatch, warm_file)

    result = mcp_server._caller_run_id({})

    assert result == "run-with-whitespace"
