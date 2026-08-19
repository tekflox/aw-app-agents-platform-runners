"""The cold path resumed an id claude had never seen, and reported success.

The warm path has distinguished create-from-resume since it was written
(`_warm_minted_session` → `--session-id`). The cold path — `_build_container_kwargs`,
which is what a run without a warm container takes — only ever emitted
`--resume`. Handed a caller-minted id, claude finds no conversation, prints
nothing, and exits 0: the run is recorded as a success with an empty reply and
zero tokens.

Reproduced end to end before this fix, against the live platform:

    POST /api/agents/watch-sonnet/run_sync  session_id=710ca728-…
    → run 5c4ba1bb…  status=success  tokens_in=0  output.text=""  session_id=null

That is also why the flag has to be carried explicitly rather than inferred
here: only agents-platform knows whether a Run has ever run under this id.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

SESSION = "710ca728-30a6-4aed-b4ec-ce16815fde5d"
BASE_JOB = {"run_id": "r1", "cli": "claude", "agent_id": "agent-1", "prompt": "hi"}


def _argv(job: dict) -> list[str]:
    _image, argv, _kwargs, _x = execute_mod._build_container_kwargs(job)
    return argv


def test_a_minted_session_is_created_on_the_cold_path():
    argv = _argv({**BASE_JOB, "session_id": SESSION, "new_session": True})
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == SESSION
    assert "--resume" not in argv


def test_an_existing_session_still_resumes():
    """Turn 2+ must resume, or the conversation silently restarts empty."""
    argv = _argv({**BASE_JOB, "session_id": SESSION})
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == SESSION
    assert "--session-id" not in argv


def test_no_session_emits_neither_flag():
    argv = _argv(dict(BASE_JOB))
    assert "--resume" not in argv and "--session-id" not in argv


def test_codex_ignores_the_flag():
    """Only claude has `--session-id`; codex keeps its resume subcommand."""
    argv = _argv({**BASE_JOB, "cli": "codex", "session_id": SESSION,
                  "new_session": True})
    assert "--session-id" not in argv
    assert "resume" in argv
