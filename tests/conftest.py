"""Suite-wide defaults.

Run: .venv/aw/bin/python -m pytest tests/
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_home_creds_copy(monkeypatch):
    """Stop tests from copying the machine's real ``$HOME`` creds anywhere.

    Any test that builds container kwargs points WORKSPACE_CONTAINER_DIR at a
    tmp_path, and `_build_container_kwargs` then calls
    `_sync_home_creds_into_workspace`, which `cp -a`s the process's REAL
    ``~/.claude`` into it. On a live workspace that directory is ~122MB, so
    each such test wrote 122MB into its own tmp dir — 79 of them per run,
    ~1.2GB, and pytest keeps the last 3 runs. Found on 2026-08-14 as 2.4GB
    sitting in the runner's shared /tmp (`data/agents-platform-runners/
    sandbox-tmp`), on a BYOD disk that has run near-full before.

    Most tests here assert on argv and mount SHAPE, for which the copy is
    pure cost. The handful that genuinely exercise creds staging
    (test_creds_mount.py, test_permission_mounts.py, test_git_creds_mount.py)
    already monkeypatch this same name themselves, and a test-local
    monkeypatch applied after this fixture still wins.
    """
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace",
                        lambda *a, **k: None)
