"""warm_pool.dispatch_turn()'s per-turn turn_env must include AW_RUN_ID
(2026-09-05, card 3d25bf3b-9510-814a-acd9-d06f9c28d10b).

Root cause: AW_RUN_ID is baked into a warm codex container's OS environment
exactly once, at container (re)creation time (execute.py's
_build_container_kwargs). dispatch_turn()'s turn_env file — the mechanism
that DOES refresh per turn, and that aw-warm-relay-codex.py's _turn_env()
overlays onto every turn's subprocess env — used to only ever write
NOTION_TASK_ID and AW_SOURCE_DEVICE, so a reused warm container kept
reporting a stale (or even another turn's) AW_RUN_ID on every turn after the
one that (re)created it. Confirmed live: turns 3-4 of a 4-turn session both
reported turn 2's run id.

Run: .venv/aw/bin/python -m pytest tests/test_dispatch_turn_run_id_env.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402


def _fake_client_and_container():
    container = MagicMock()
    container.exec_run.return_value = (0, b"")

    client = MagicMock()
    client.containers.get.return_value = container

    sock = MagicMock()
    sock._sock = MagicMock()
    client.api.exec_create.return_value = {"Id": "exec-1"}
    client.api.exec_start.return_value = sock

    return client, container


def test_turn_env_includes_aw_run_id_for_this_turn():
    client, container = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-agent-session", run_id="turn-3-run-id",
        prompt="hi", cli="codex", notion_task_id="task-1", source_device="telegram",
    )

    setup_cmd = container.exec_run.call_args[0][0]
    assert setup_cmd[0:2] == ["sh", "-c"]
    script = setup_cmd[2]
    assert "AW_RUN_ID='turn-3-run-id'" in script or 'turn-3-run-id' in script

    # The turn_env payload itself (piped into the container's turn_env file)
    # is embedded as a shell-quoted argument to `printf` — pull it out and
    # check the exported var, not just that the run id string appears
    # somewhere (current_run_id also contains it).
    assert "export AW_RUN_ID=" in script
    run_id_line = next(
        line for line in script.splitlines() if "export AW_RUN_ID=" in line
    )
    assert "turn-3-run-id" in run_id_line


def test_turn_env_still_includes_notion_task_id_and_source_device():
    client, container = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-agent-session", run_id="r1",
        prompt="hi", cli="codex", notion_task_id="task-42", source_device="telegram",
    )

    script = container.exec_run.call_args[0][0][2]
    assert "export NOTION_TASK_ID='task-42'" in script or "task-42" in script
    assert "export AW_SOURCE_DEVICE='telegram'" in script or "telegram" in script
