"""warm_pool.dispatch_turn()'s claude branch must carry each turn's own
NOTION_TASK_ID/AW_RUN_ID/AW_SOURCE_DEVICE, even though the claude CLI
process's OS env cannot be (card 3d65bf3b-9510-81b0-8b50-d8f3b74f4374).

Root cause: unlike codex — a fresh `codex exec resume` subprocess per turn,
whose env aw-warm-relay-codex.py's `_turn_env()` refreshes from
`turn_env` (see test_dispatch_turn_run_id_env.py, the AW_RUN_ID precedent for
codex) — claude's `aw-warm-wrapper` spawns ONE long-lived claude process for
the container's whole life. Nothing can push an updated env var into an
already-running process, so `turn_env` has no reader on the claude side at
all: a claude warm container's $NOTION_TASK_ID/$AW_RUN_ID/$AW_SOURCE_DEVICE
stay pinned to whatever turn (re)created the container, for every later turn.

Fix mirrors agents-platform-multitenant's own (separate) CliLLM warm path,
which hit the identical problem and shipped this exact workaround after
BASH_ENV proved a dead end (the Bash tool spawns via `/bin/sh`, which does
not source it): put the current turn's identity directly in the prompt text
`dispatch_turn` feeds into the FIFO, so the model has the correct values
regardless of what any later `echo $NOTION_TASK_ID` reports.

Run: python3 -m pytest tests/test_dispatch_turn_claude_context.py
"""
from __future__ import annotations

import json
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

    return client, container, sock


def _sent_content(sock) -> str:
    """The claude stream-json envelope's user-message content, decoded back
    out of whatever dispatch_turn() sent over the FIFO's raw socket."""
    raw = b"".join(c.args[0] for c in sock._sock.sendall.call_args_list)
    return json.loads(raw.decode("utf-8"))["message"]["content"]


def test_claude_turn_carries_this_turns_notion_task_id():
    client, _container, sock = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-claude-session", run_id="turn-2-run-id",
        prompt="do the thing", cli="claude",
        notion_task_id="task-turn-2", source_device="telegram",
    )

    content = _sent_content(sock)
    assert "NOTION_TASK_ID=task-turn-2" in content
    assert "AW_RUN_ID=turn-2-run-id" in content
    assert "AW_SOURCE_DEVICE=telegram" in content
    assert content.endswith("do the thing")


def test_different_turns_of_the_same_session_carry_their_own_values():
    """The exact staleness scenario: turn 1 creates the container with one
    card, turn 2 of the SAME session dispatches with a different (or no)
    card — each turn's own FIFO payload must reflect ITS OWN value, not the
    previous turn's, since nothing about dispatch_turn's own state carries
    over between calls."""
    client, _container, sock1 = _fake_client_and_container()
    warm_pool.dispatch_turn(
        client=client, name="aw-warm-claude-session", run_id="turn-1",
        prompt="turn one", cli="claude",
        notion_task_id="task-A", source_device="telegram",
    )
    assert "NOTION_TASK_ID=task-A" in _sent_content(sock1)

    client2, _container2, sock2 = _fake_client_and_container()
    warm_pool.dispatch_turn(
        client=client2, name="aw-warm-claude-session", run_id="turn-2",
        prompt="turn two", cli="claude",
        notion_task_id="task-B", source_device="telegram",
    )
    content2 = _sent_content(sock2)
    assert "NOTION_TASK_ID=task-B" in content2
    assert "task-A" not in content2

    client3, _container3, sock3 = _fake_client_and_container()
    warm_pool.dispatch_turn(
        client=client3, name="aw-warm-claude-session", run_id="turn-3",
        prompt="turn three", cli="claude",
        notion_task_id=None, source_device=None,
    )
    content3 = _sent_content(sock3)
    assert "NOTION_TASK_ID=" not in content3
    assert "task-B" not in content3
    assert "AW_RUN_ID=turn-3" in content3
    assert content3.endswith("turn three")


def test_codex_prompt_is_unmodified_plain_json():
    """The context-injection workaround is claude-only — codex already gets
    a correct, per-turn-fresh env via aw-warm-relay-codex.py's _turn_env()
    (see test_dispatch_turn_run_id_env.py), so wrapping codex's prompt the
    same way would just be noise codex has no use for."""
    client, _container, sock = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-codex-session", run_id="r1",
        prompt="hi", cli="codex", notion_task_id="task-1", source_device="telegram",
    )

    raw = b"".join(c.args[0] for c in sock._sock.sendall.call_args_list)
    assert json.loads(raw.decode("utf-8")) == {"prompt": "hi"}


# ---------------------------------------------------------------------------
# …and must NOT carry it on a raw turn
# ---------------------------------------------------------------------------
# The header above is correct for an ordinary turn and wrong for a raw one.
# A raw turn is a CLI slash command; the claude CLI only recognises one at
# position 0 of the prompt, and this header puts it at ~position 230. The
# model then answers "/compact" as a chat message and nothing is compacted.
#
# Live proof, session 4d86e8e6-85c1-4759-86c5-f4e7e47b58fe: nine
# initiator_kind='auto_compact' runs between 2026-09-10 09:20Z and
# 2026-09-11 08:20Z wrote no `compact_boundary` at all. Transcript line 3131
# is what they wrote instead — "*Sem resposta necessária — `/compact` é
# comando de nível do harness*" — against a 542,314-token context, billed.
# It is 2026-07-05's `ap-auto-compact-not-compacting` root cause #1 (framing
# displaces the slash command), reproduced in this app.
#
# Re-proved live on 2026-09-11 AFTER agents-platform-multitenant fixed the
# identical prepend in its OWN CliLLM warm path: a probe run through this
# Runner still came back "Understood. I've compacted the conversation
# context…" with no boundary, because THIS is the prepend that is actually on
# the live path. Two implementations of the same workaround, one of them
# dormant — fixing only the dormant one is a green no-op.

def test_a_raw_turn_reaches_the_container_at_position_zero():
    client, _container, sock = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-claude-session", run_id="compact-run",
        prompt="/compact", cli="claude",
        notion_task_id="task-1", source_device="telegram", raw_prompt=True,
    )

    content = _sent_content(sock)
    assert content == "/compact", (
        "a slash command must be the WHOLE prompt — anything prepended turns "
        "it into a question the model answers instead of a command the CLI runs"
    )
    assert "[SYSTEM]" not in content
    assert "Execution context for this turn" not in content


def test_a_raw_clear_is_not_reframed_either():
    """`/clear` is not dispatched raw by agents-platform today (it is a
    verified headless no-op, so it is implemented as a fresh session binding
    instead). The defect being pinned is in this prepend, not in `/compact` —
    asserting only `/compact` would let the same displacement ship for the
    next raw command routed through here, which is how this class of bug has
    already recurred twice."""
    client, _container, sock = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-claude-session", run_id="clear-run",
        prompt="/clear", cli="claude", notion_task_id="task-1",
        source_device="telegram", raw_prompt=True,
    )

    assert _sent_content(sock) == "/clear"


def test_an_ordinary_turn_is_unaffected_by_the_raw_flag_defaulting_off():
    """The flag is absent on an older agents-platform's job body, and absent
    must keep meaning "ordinary turn, add the header" — the header is still
    the only way a warm claude turn learns its own NOTION_TASK_ID."""
    client, _container, sock = _fake_client_and_container()

    warm_pool.dispatch_turn(
        client=client, name="aw-warm-claude-session", run_id="turn-9",
        prompt="do the thing", cli="claude",
        notion_task_id="task-9", source_device="telegram",
    )

    content = _sent_content(sock)
    assert content.startswith("[SYSTEM]\nExecution context for this turn: ")
    assert "NOTION_TASK_ID=task-9" in content
    assert content.endswith("do the thing")
