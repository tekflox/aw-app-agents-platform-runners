"""A large system prompt must not be handed to the CLI as an argv string.

Card 4185bf3b-9510-812e-831e-f160c2f26a37. Every `manager` agent dispatch
failed 3/3 — 2026-11-13T23:09:21Z, 2026-11-14T00:39:42Z, 2026-11-14T00:41:53Z
— with zero tokens in and out, no LLM turn, and this on the run timeline
within ~2s of node_start:

    exec container process `/opt/agent-npm/bin/claude`: Argument list too long

That is runc's E2BIG wrapper. Linux caps EACH argv string at
``MAX_ARG_STRLEN`` (a hardcoded 32 x PAGE_SIZE = 131,072 bytes), separately
from the much larger total ARG_MAX budget, and the kernel refuses the exec
before the binary runs — hence a container that starts, dies, and bills
nothing. Reproduced at the docker layer while fixing this: a single 131,072+
byte argument fails, 131,000 runs fine.

The oversized argument was ``--append-system-prompt <value>``, where the
value is what agents-platform-multitenant's executor.py assembles for an
agent: its own system_prompt, plus the FULL text of every skill in
``skill_slugs``, plus Agents Flow context. For `manager` that measured
~150,871 bytes (the `aw-manager` SKILL.md alone had grown to 129,123 bytes
over a day of incremental PRs, 79,098 -> 129,123). Nothing was wrong with the
skill: argv was being used as an implicit size cap on agent instructions, and
the content merely decided which day it tripped.

Both spawn paths had it, so both are pinned here:

* the COLD path (``_build_container_kwargs``) — every turn-1/fresh-session
  dispatch, which is every sweep iteration;
* the WARM path (``_build_warm_kwargs_claude``) — the container's creation
  argv, so an E2BIG there takes out every turn of that session, not just the
  first.

Run: python3 -m pytest tests/test_append_system_prompt_file.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

#: Past the kernel's per-argument limit on its own, the way the live
#: `manager` system prompt was. Deliberately a real over-the-line size
#: rather than a token "big string" — the point of the test is the boundary.
OVERSIZED = "SKILL CONTENT LINE — padding to cross MAX_ARG_STRLEN.\n" * 2800

BASE_JOB = {"run_id": "r-e2big", "cli": "claude", "agent_id": "agent-1",
            "session_id": "710ca728-30a6-4aed-b4ec-ce16815fde5d",
            "prompt": "selector: stale-sweep"}


def _setup(tmp_path, monkeypatch) -> None:
    """Same isolation the other builder suites use: these builders CREATE the
    run's scratch dir on disk, so without redirecting it the test writes into
    the real workspace (and fails outright on a CI runner with no
    /opt/aw-workspace)."""
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    # Forces the fallback (non-direct-home) credential branch, which is the
    # one that mounts the isolated run dir explicitly — so the asserted
    # container path is the one really bound in.
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")


def _cold(job: dict, tmp_path, monkeypatch) -> tuple[list[str], dict]:
    _setup(tmp_path, monkeypatch)
    _image, argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return argv, kwargs


def _warm(job: dict, tmp_path, monkeypatch) -> tuple[list[str], dict]:
    _setup(tmp_path, monkeypatch)
    _image, kwargs = execute_mod._build_warm_kwargs_claude(
        job, epoch_hash="epoch1", redis_url="redis://example.test:6379/0")
    return kwargs["command"], kwargs


def _cold_argv(job: dict, tmp_path, monkeypatch) -> list[str]:
    return _cold(job, tmp_path, monkeypatch)[0]


def _warm_argv(job: dict, tmp_path, monkeypatch) -> list[str]:
    return _warm(job, tmp_path, monkeypatch)[0]


def _oversized(argv: list[str]) -> list[str]:
    return [a for a in argv if len(a.encode("utf-8")) >= execute_mod.MAX_ARG_STRLEN]


def test_the_padding_this_test_uses_would_really_have_e2bigd():
    """Guards the guard: if OVERSIZED ever drifts under the kernel limit,
    every assertion below still passes and proves nothing."""
    assert len(OVERSIZED.encode("utf-8")) >= execute_mod.MAX_ARG_STRLEN
    assert execute_mod.MAX_ARG_STRLEN == 131_072


@pytest.mark.parametrize("build", [_cold_argv, _warm_argv])
def test_an_oversized_system_prompt_leaves_argv_entirely(build, tmp_path, monkeypatch):
    argv = build({**BASE_JOB, "session_id": "sess-1", "new_session": True,
                  "append_system_prompt": OVERSIZED}, tmp_path, monkeypatch)

    assert _oversized(argv) == [], (
        "an argv string at or past MAX_ARG_STRLEN — execve() refuses this "
        "container with E2BIG before the CLI starts, and the run bills zero "
        "tokens with no LLM turn"
    )
    assert "--append-system-prompt" not in argv
    assert "--append-system-prompt-file" in argv


@pytest.mark.parametrize("build", [_cold, _warm])
def test_the_prompt_reaches_the_container_verbatim_by_path(build, tmp_path, monkeypatch):
    """Keeping it out of argv is only half of it — the CLI has to actually be
    able to read the same bytes back, or the agent silently runs with no
    persona (the failure mode the inline flag exists to prevent)."""
    argv, kwargs = build({**BASE_JOB, "append_system_prompt": OVERSIZED},
                         tmp_path, monkeypatch)

    container_path = argv[argv.index("--append-system-prompt-file") + 1]
    assert container_path == (
        f"/home/ubuntu/.claude/isolated/{BASE_JOB['run_id']}/"
        f"{execute_mod.APPEND_SYSTEM_PROMPT_FILENAME}")

    # Resolve the file through the MOUNT TABLE rather than trusting the write
    # site: that a file exists somewhere on the host proves nothing about
    # whether the container can open the path we just put in argv. The bind
    # source is a host-side path (podman resolves it, not this process), so
    # translate it back through the host->container-dir mapping to read it.
    mount_dir = str(Path(container_path).parent)
    host_dir = next(h for h, m in kwargs["volumes"].items() if m["bind"] == mount_dir)
    local_dir = host_dir.replace(execute_mod.WORKSPACE_HOST_DIR.rstrip("/"),
                                 execute_mod.WORKSPACE_CONTAINER_DIR.rstrip("/"), 1)
    assert (Path(local_dir) / execute_mod.APPEND_SYSTEM_PROMPT_FILENAME
            ).read_text(encoding="utf-8") == OVERSIZED


@pytest.mark.parametrize("build", [_cold_argv, _warm_argv])
def test_a_small_system_prompt_takes_the_same_path(build, tmp_path, monkeypatch):
    """No size-dependent branch. A threshold would leave the file transport
    unexercised by almost every real dispatch, and make the one path that
    matters the one nobody runs."""
    argv = build({**BASE_JOB, "append_system_prompt": "You are the Manager."},
                 tmp_path, monkeypatch)

    assert "--append-system-prompt-file" in argv
    assert "--append-system-prompt" not in argv


@pytest.mark.parametrize("build", [_cold_argv, _warm_argv])
def test_no_system_prompt_emits_no_flag_at_all(build, tmp_path, monkeypatch):
    argv = build(dict(BASE_JOB), tmp_path, monkeypatch)

    assert "--append-system-prompt-file" not in argv
    assert "--append-system-prompt" not in argv


def test_a_failed_stage_falls_back_to_the_inline_flag(tmp_path, monkeypatch):
    """A persona that MIGHT not fit beats a run that is silently a different
    agent. The fallback is loud (log.error naming the byte count) so the next
    occurrence is diagnosable from the runner's log instead of a bare runc
    E2BIG — but it still has to produce a usable argv."""
    _setup(tmp_path, monkeypatch)

    def _boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", _boom)
    argv = execute_mod._append_system_prompt_args(
        {**BASE_JOB, "append_system_prompt": "You are the Manager."},
        execute_mod.CLI_SPECS["claude"])

    assert argv == ["--append-system-prompt", "You are the Manager."]


def test_codex_still_gets_its_persona_prepended_into_the_prompt(tmp_path, monkeypatch):
    """codex has neither flag (checked against codex-cli 0.147.0), so its
    system prompt rides in the prompt text — unchanged by this fix, and still
    argv-bound. Pinned so the behaviour is explicit rather than assumed.
    """
    argv = _cold_argv({**BASE_JOB, "cli": "codex",
                       "append_system_prompt": "You are the Manager."},
                      tmp_path, monkeypatch)

    assert "--append-system-prompt" not in argv
    assert "--append-system-prompt-file" not in argv
    assert any("You are the Manager." in a for a in argv)
