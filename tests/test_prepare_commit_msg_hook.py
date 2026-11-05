"""Shell-level integration coverage for agent-images/shared/git-hooks/
(card 40f5bf3b-9510-811d-916c-dd2ad57d3ac4; design on card
40f5bf3b-9510-8174-be1c-c69bdc2676ba).

The hooks themselves are POSIX sh, not Python (the shell image has no
node), so this drives real git subprocesses against a throwaway repo rather
than mocking anything — the only way to actually exercise
`core.hooksPath` + `git interpret-trailers` end to end.

The mutation-sensitive case this file exists to catch: a naive
$AW_RUN_ID-only (or $NOTION_TASK_ID-only) implementation would silently
re-stamp turn 1's stale identity on every later turn of a REUSED warm
container, because execute.py only bakes those into the container's OS
environment once, at (re)creation time — see
test_dispatch_turn_run_id_env.py's docstring for the exact precedent this
mirrors on the Python side. `test_warm_reuse_prefers_this_turns_identity_
over_the_containers_stale_baked_env` reproduces that exact shape: a
"turn 1" identity baked into process env, a "turn 2" identity written to
the warm files the way warm_pool.dispatch_turn actually writes them, and
asserts the commit carries turn 2's identity, not turn 1's.

Run: /usr/bin/python3.12 -m pytest tests/test_prepare_commit_msg_hook.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOOKS_DIR = ROOT / "agent-images" / "shared" / "git-hooks"

# The ambient env this very test process may itself be running under (a
# live aw-* agent container has AW_RUN_ID/AW_SESSION_ID/NOTION_TASK_ID set,
# and a live warm container even has a real /home/ubuntu/.aw-warm) must
# never leak into these subprocess calls — every test's intent is exactly
# which identity IS or ISN'T visible to the hook.
#
# GIT_CONFIG_* is its own case, not just another identity var: since this
# feature went live, every github-permitted runner/Coder/QA container (this
# test's own process very possibly included — found live by QA reviewing
# this exact file, card 40f5bf3b-9510-811d-916c-dd2ad57d3ac4) has
# GIT_CONFIG_COUNT/KEY_0/VALUE_0 set in its ambient env, pointing at the
# production-mounted /opt/aw-agent/git-hooks — which OUTRANKS a repo's own
# `git config core.hooksPath` (env config beats file config). Left
# unstripped, _init_repo()'s own `git config core.hooksPath HOOKS_DIR` call
# is silently overridden by the ambient ones, so most of this file's tests
# stop exercising the repo's checked-out hook code and instead run whatever
# happens to already be mounted in the container — a false "tests pass"
# that would not have caught a real regression in prepare-commit-msg's own
# identity-resolution logic. A whole GIT_CONFIG_* prefix is stripped, not
# just these three names, since GIT_CONFIG_COUNT controls how many
# GIT_CONFIG_KEY_N/VALUE_N pairs git reads — leaving a stray one behind
# would be read as a 4th, garbled entry.
_IDENTITY_VARS = ("AW_RUN_ID", "AW_SESSION_ID", "NOTION_TASK_ID", "AW_WARM_DIR",
                  "AW_SOURCE_DEVICE", "AW_GATEWAY_BEARER_TOKEN")


def _clean_env(tmp_path, **overrides) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _IDENTITY_VARS and not k.startswith("GIT_CONFIG_")}
    env["HOME"] = str(tmp_path)  # git needs a writable $HOME for its own config lookups
    env.setdefault("AW_WARM_DIR", str(tmp_path / "no-such-warm-dir"))
    env.update(overrides)
    return env


def _git(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, timeout=10,
        capture_output=True, text=True,
    )


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _clean_env(tmp_path)
    _git(repo, env, "init", "-q")
    _git(repo, env, "config", "user.email", "test@test.com")
    _git(repo, env, "config", "user.name", "Test")
    _git(repo, env, "config", "core.hooksPath", str(HOOKS_DIR))
    return repo


def _commit(repo: Path, tmp_path, filename: str, message: str, **env_overrides) -> subprocess.CompletedProcess:
    (repo / filename).write_text("content\n")
    env = _clean_env(tmp_path, **env_overrides)
    add = _git(repo, env, "add", filename)
    assert add.returncode == 0, add.stderr
    return _git(repo, env, "commit", "-q", "-m", message)


def _last_message(repo: Path, tmp_path) -> str:
    env = _clean_env(tmp_path)
    out = _git(repo, env, "log", "-1", "--format=%B")
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_cold_path_uses_aw_run_id_env_fallback(tmp_path):
    repo = _init_repo(tmp_path)

    result = _commit(repo, tmp_path, "a.txt", "first",
                      AW_RUN_ID="coldrun123", AW_SESSION_ID="sess1", NOTION_TASK_ID="card1")

    assert result.returncode == 0, result.stderr
    msg = _last_message(repo, tmp_path)
    assert "AW-Run-Id: coldrun123" in msg
    assert "AW-Session-Id: sess1" in msg
    assert "AW-Card: card1" in msg


def test_no_identity_available_is_a_silent_noop(tmp_path):
    """A human committing from outside a runner container (or any context
    that never set AW_RUN_ID/current_run_id up) must see no trailers and no
    error — the hook has to behave as if it doesn't exist."""
    repo = _init_repo(tmp_path)

    result = _commit(repo, tmp_path, "a.txt", "first")

    assert result.returncode == 0, result.stderr
    msg = _last_message(repo, tmp_path)
    assert "AW-Run-Id:" not in msg
    assert msg.strip() == "first"


def test_warm_path_prefers_current_run_id_file_over_stale_env(tmp_path):
    """The exact staleness shape fixed for codex's $AW_RUN_ID in card
    3d25bf3b-9510-814a-acd9-d06f9c28d10b: a reused warm container's baked
    env still says the FIRST turn's run id, but the per-turn warm file
    (rewritten by warm_pool.dispatch_turn every turn) says the current one.
    The file must win."""
    repo = _init_repo(tmp_path)
    warm_dir = tmp_path / "warm"
    warm_dir.mkdir()
    (warm_dir / "current_run_id").write_bytes(b"freshrun456")

    result = _commit(repo, tmp_path, "a.txt", "second",
                      AW_WARM_DIR=str(warm_dir), AW_RUN_ID="STALE_ENV_RUN")

    assert result.returncode == 0, result.stderr
    msg = _last_message(repo, tmp_path)
    assert "AW-Run-Id: freshrun456" in msg
    assert "STALE_ENV_RUN" not in msg


def test_warm_reuse_prefers_this_turns_identity_over_the_containers_stale_baked_env(tmp_path):
    """Mutation-sensitive regression for warm-container reuse, mirroring
    test_dispatch_turn_run_id_env.py's precedent on the Python side.

    Simulates turn 1 (container creation): AW_RUN_ID/NOTION_TASK_ID baked
    into the process env. Then turn 2 of the SAME (reused) warm container:
    warm_pool.dispatch_turn has rewritten current_run_id AND turn_env with
    turn 2's own values — a different run id, and no card this time. A
    naive implementation reading only $AW_RUN_ID/$NOTION_TASK_ID would
    silently re-stamp turn 1's run id and turn 1's card on turn 2's commit.
    """
    repo = _init_repo(tmp_path)
    warm_dir = tmp_path / "warm"
    warm_dir.mkdir()
    # What warm_pool.dispatch_turn actually writes, every turn — see
    # warm_pool.py's own `turn_env = f"export AW_RUN_ID=... ..."` literal.
    (warm_dir / "current_run_id").write_bytes(b"turn2run")
    (warm_dir / "turn_env").write_text(
        "export AW_RUN_ID='turn2run'\nexport NOTION_TASK_ID=''\nexport AW_SOURCE_DEVICE=''\n"
    )

    result = _commit(repo, tmp_path, "a.txt", "fourth",
                      AW_WARM_DIR=str(warm_dir),
                      # Turn 1's stale, still-baked-into-this-process env:
                      AW_RUN_ID="turn1run", AW_SESSION_ID="sess1", NOTION_TASK_ID="stale-card-1")

    assert result.returncode == 0, result.stderr
    msg = _last_message(repo, tmp_path)
    assert "AW-Run-Id: turn2run" in msg
    assert "turn1run" not in msg
    # Turn 2 genuinely has no card — the stale turn-1 card must not survive.
    assert "AW-Card:" not in msg
    assert "stale-card-1" not in msg
    # Session id has no per-turn file (stable per warm container by design)
    # so it's still read straight from the env.
    assert "AW-Session-Id: sess1" in msg


def test_idempotent_replace_on_amend(tmp_path):
    repo = _init_repo(tmp_path)
    first = _commit(repo, tmp_path, "a.txt", "first", AW_RUN_ID="r1")
    assert first.returncode == 0, first.stderr

    env = _clean_env(tmp_path, AW_RUN_ID="r1-amended")
    amend = _git(repo, env, "commit", "-q", "--amend", "-m", "first amended")
    assert amend.returncode == 0, amend.stderr

    msg = _last_message(repo, tmp_path)
    assert msg.count("AW-Run-Id:") == 1
    assert "AW-Run-Id: r1-amended" in msg


def test_chains_to_existing_repo_local_pre_commit_hook(tmp_path):
    """core.hooksPath replaces .git/hooks wholesale — the §6
    pre-commit-framework standard writes its hook to .git/hooks/pre-commit,
    and this chain wrapper must still run it."""
    repo = _init_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "lint.ran"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    (hooks / "pre-commit").chmod(0o755)

    result = _commit(repo, tmp_path, "a.txt", "fifth", AW_RUN_ID="r5")

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "AW-Run-Id: r5" in _last_message(repo, tmp_path)


def test_failing_chained_hook_aborts_the_commit(tmp_path):
    """A failing repo-local prepare-commit-msg must still abort the commit,
    same as if core.hooksPath had never been overridden — our stamping must
    not silently swallow that failure."""
    repo = _init_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "prepare-commit-msg").write_text("#!/bin/sh\nexit 7\n")
    (hooks / "prepare-commit-msg").chmod(0o755)

    result = _commit(repo, tmp_path, "a.txt", "sixth", AW_RUN_ID="r6")

    assert result.returncode != 0


def test_husky_style_repo_local_hooks_path_still_fires_under_env_override(tmp_path):
    """Production activation is GIT_CONFIG_COUNT/KEY_0/VALUE_0 env vars
    (execute.py), which outrank a repo-local core.hooksPath from git's own
    point of view. A repo that configures its own hook manager (husky etc.)
    must still have it fire — chain_hook reads the repo's .git/config file
    directly to find it, bypassing the env override that shadows it."""
    repo = _init_repo(tmp_path)
    # Undo the plain `git config core.hooksPath` _init_repo set, and set a
    # DIFFERENT repo-local path instead, to isolate this from the loop-guard
    # scenario covered below.
    env = _clean_env(tmp_path)
    _git(repo, env, "config", "core.hooksPath", ".husky")
    husky = repo / ".husky"
    husky.mkdir()
    marker = tmp_path / "husky.ran"
    (husky / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    (husky / "pre-commit").chmod(0o755)

    result = _commit(repo, tmp_path, "a.txt", "seventh", AW_RUN_ID="r7",
                      GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                      GIT_CONFIG_VALUE_0=str(HOOKS_DIR))

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "husky-style repo-local hook never fired"
    assert "AW-Run-Id: r7" in _last_message(repo, tmp_path)


def test_repo_local_hooks_path_pointed_at_our_own_dir_does_not_loop(tmp_path):
    """Regression for the exact failure this file's author hit by hand
    while writing these hooks: if a repo's OWN .git/config ever names this
    same hooks directory as its core.hooksPath (not via the env override,
    which never touches .git/config, but some other means), chain_hook must
    not exec this very script again — that recursion has no base case and
    will exhaust the process table. subprocess's own `timeout=10` in
    `_git()` is this test's own backstop; a regression here hangs/fork-bombs
    rather than failing an assertion."""
    repo = _init_repo(tmp_path)  # _init_repo already sets core.hooksPath = HOOKS_DIR

    result = _commit(repo, tmp_path, "a.txt", "eighth", AW_RUN_ID="r8")

    assert result.returncode == 0, result.stderr
    assert "AW-Run-Id: r8" in _last_message(repo, tmp_path)


@pytest.mark.parametrize("name", [
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
    "pre-merge-commit", "commit-msg", "post-commit", "pre-rebase",
    "post-checkout", "post-merge", "pre-push", "pre-auto-gc", "post-rewrite",
    "sendemail-validate", "fsmonitor-watchman", "prepare-commit-msg",
])
def test_every_shipped_hook_is_executable_and_posix_sh(name):
    path = HOOKS_DIR / name
    assert path.is_file(), f"{name} missing from {HOOKS_DIR}"
    assert os.access(path, os.X_OK), f"{name} is not executable"
    check = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
