"""Regression test: _build_warm_kwargs's claude_argv used to be built from
scratch and silently dropped allowed_tools/disallowed_tools/
append_system_prompt/dangerous_skip_permissions — flags the cold path
(_build_container_kwargs) already wires in from the exact same job dict.
Turn 1 of a session is always cold (no session_id yet to key a warm
container on), so this only showed up starting turn 2: a job with
dangerous_skip_permissions=True (or defaulted) still spawned its warm
container WITHOUT --dangerously-skip-permissions, so Claude Code's
interactive permission gate kicked in for real on a supposedly-unattended
runner ("This command requires approval"). Found live 2026-08-11.

Run: .venv/aw/bin/python -m pytest tests/test_warm_kwargs_argv_parity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

BASE_JOB = {
    "run_id": "r1",
    "cli": "claude",
    "agent_id": "agent-1",
    "session_id": "session-1",
    "prompt": "hi",
}


def _warm_argv(job: dict, tmp_path: Path, monkeypatch) -> list[str]:
    # _build_container_kwargs (called internally by _build_warm_kwargs)
    # writes the isolated-run mcp.json under WORKSPACE_CONTAINER_DIR — point
    # it at a tmp dir instead of the real /opt/aw-workspace this test has no
    # write access to (same pattern as tests/test_creds_mount.py).
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    _image, kwargs = execute_mod._build_warm_kwargs(job, "epoch1", "redis://example.test:6379/0")
    return kwargs["command"]


def test_dangerous_skip_permissions_defaults_true_in_warm_mode(tmp_path, monkeypatch):
    argv = _warm_argv(dict(BASE_JOB), tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" in argv


def test_dangerous_skip_permissions_false_is_honored_in_warm_mode(tmp_path, monkeypatch):
    argv = _warm_argv({**BASE_JOB, "dangerous_skip_permissions": False}, tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" not in argv


def test_allowed_and_disallowed_tools_forwarded_in_warm_mode(tmp_path, monkeypatch):
    argv = _warm_argv({
        **BASE_JOB,
        "allowed_tools": ["Read", "Grep"],
        "disallowed_tools": ["Bash"],
    }, tmp_path, monkeypatch)
    assert "--allowed-tools" in argv
    assert argv[argv.index("--allowed-tools") + 1] == "Read,Grep"
    assert "--disallowed-tools" in argv
    assert argv[argv.index("--disallowed-tools") + 1] == "Bash"


def test_append_system_prompt_forwarded_in_warm_mode(tmp_path, monkeypatch):
    argv = _warm_argv({**BASE_JOB, "append_system_prompt": "extra rules"}, tmp_path, monkeypatch)
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "extra rules"


def test_secure_mode_style_job_gets_no_bypass_flag_and_keeps_bash_disallowed(tmp_path, monkeypatch):
    # Mirrors what agents-platform-multitenant's make_llm() sends for
    # security_mode="secure": dangerous_skip_permissions=False + "Bash"
    # appended to disallowed_tools.
    argv = _warm_argv({
        **BASE_JOB,
        "dangerous_skip_permissions": False,
        "disallowed_tools": ["Bash"],
    }, tmp_path, monkeypatch)
    assert "--dangerously-skip-permissions" not in argv
    assert "--disallowed-tools" in argv
    assert argv[argv.index("--disallowed-tools") + 1] == "Bash"
