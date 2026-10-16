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
from agents_platform_runners_app import execution_index as execution_index_mod  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_execution_index_counter_redis(monkeypatch):
    """Stop every test that calls ``index_run``/``start_after_stream_done``
    from making ``_incr`` reach out over the network.

    ``_counter_redis_client`` falls back to ``shared_redis.resolve``'s real
    gateway-probing discovery when no URL is configured — fine in production
    (a one-time, cached probe), but every test in this file that exercises
    the indexing hook would otherwise pay that probe's latency once per
    process and, on a host with something listening on 6379, could start
    incrementing real counters on whatever Redis it finds. ``_incr``/
    ``counters_snapshot`` are fail-open by design, so defaulting the client
    to None here changes no test's assertions — only the counter tests in
    test_execution_index_counters.py override this themselves."""
    monkeypatch.setattr(execution_index_mod, "_counter_redis_client", lambda: None)


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
