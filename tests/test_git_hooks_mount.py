"""Regression coverage for the run-id provenance hook's mount + activation
(card 40f5bf3b-9510-811d-916c-dd2ad57d3ac4; design on card
40f5bf3b-9510-8174-be1c-c69bdc2676ba).

A container that can push (Agent Config's "GitHub / Git" permission) gets
agent-images/shared/git-hooks/ mounted read-only at
/opt/aw-agent/git-hooks, and GIT_CONFIG_COUNT/GIT_CONFIG_KEY_0/
GIT_CONFIG_VALUE_0 set to activate it via core.hooksPath — gated on the
SAME _perms["github"] check that already gates the git-creds mount, so the
two can never disagree about whether a run can push. The raw monitor-run
path (_build_raw_kwargs) has no Agent Config at all, so it mounts and
activates unconditionally, paired with its own unconditional git-creds
mount.

The hook scripts' own logic (identity resolution, chaining, trailer
stamping) is covered separately in test_prepare_commit_msg_hook.py, which
drives real git — this file only covers whether the mount/env wiring that
turns the hooks dir ON is correct.

Run: /usr/bin/python3.12 -m pytest tests/test_git_hooks_mount.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


def _base_job(**overrides) -> dict:
    job = {"run_id": "r1", "cli": "claude", "prompt": "hi"}
    job.update(overrides)
    return job


def _setup(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home-unused"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")  # -> claude fallback, irrelevant here
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace", lambda *a, **k: None)
    return ws


def _expected_hooks_source() -> str:
    """Independently computed expected mount source — if _host_path_for's
    WORKSPACE_CONTAINER_DIR -> WORKSPACE_HOST_DIR prefix swap ever breaks for
    this new call site, this must break with it rather than the test loosely
    matching on `bind` alone and missing a mount pointed at the wrong host
    path (which would silently never run the hook — no error anywhere)."""
    return execute_mod._host_path_for(execute_mod.GIT_HOOKS_DIR)


def test_github_permission_on_mounts_hooks_and_activates_them(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(
        _base_job(permissions={"github": True}))

    assert kwargs["volumes"][_expected_hooks_source()] == {
        "bind": execute_mod.GIT_HOOKS_CONTAINER_MOUNT, "mode": "ro"}
    env = kwargs["environment"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == execute_mod.GIT_HOOKS_CONTAINER_MOUNT


def test_github_permission_off_mounts_no_hooks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(
        _base_job(permissions={"github": False}))

    assert not any(v["bind"] == execute_mod.GIT_HOOKS_CONTAINER_MOUNT
                   for v in kwargs["volumes"].values())
    assert "GIT_CONFIG_COUNT" not in kwargs["environment"]
    assert "GIT_CONFIG_KEY_0" not in kwargs["environment"]
    assert "GIT_CONFIG_VALUE_0" not in kwargs["environment"]


def test_no_permissions_key_mounts_no_hooks(tmp_path, monkeypatch):
    """Same contract as the github-permission-off case, but for a job with
    no "permissions" key at all — mirrors test_git_creds_mount.py's own
    no-permissions-key coverage for the creds mount."""
    _setup(tmp_path, monkeypatch)

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(_base_job())

    assert not any(v["bind"] == execute_mod.GIT_HOOKS_CONTAINER_MOUNT
                   for v in kwargs["volumes"].values())
    assert "GIT_CONFIG_COUNT" not in kwargs["environment"]


def test_raw_command_mounts_and_activates_hooks_unconditionally(tmp_path, monkeypatch):
    """A monitor run has no Agent Config, hence no permissions.github to
    gate on — it gets the hooks mount+activation unconditionally, exactly
    like it already gets the git creds mount unconditionally."""
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))

    _image, _argv, kwargs = execute_mod._build_raw_kwargs(
        {"run_id": "abc123", "raw_command": "echo hi"})

    assert kwargs["volumes"][_expected_hooks_source()] == {
        "bind": execute_mod.GIT_HOOKS_CONTAINER_MOUNT, "mode": "ro"}
    env = kwargs["environment"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == execute_mod.GIT_HOOKS_CONTAINER_MOUNT


def test_warm_claude_kwargs_inherit_the_hooks_mount_and_activation(tmp_path, monkeypatch):
    """_build_warm_kwargs_claude builds on top of _build_container_kwargs's
    own kwargs rather than constructing its volumes/env from scratch — a
    warm (session-reused) container must get the same provenance stamping a
    cold one does, not a second, divergent code path that could drift."""
    _setup(tmp_path, monkeypatch)

    image, kwargs = execute_mod._build_warm_kwargs_claude(
        _base_job(agent_id="agent-1", session_id="sess-1",
                  permissions={"github": True}),
        epoch_hash="epoch1", redis_url="redis://example.test:6379/0")

    assert kwargs["volumes"][_expected_hooks_source()] == {
        "bind": execute_mod.GIT_HOOKS_CONTAINER_MOUNT, "mode": "ro"}
    env = kwargs["environment"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_VALUE_0"] == execute_mod.GIT_HOOKS_CONTAINER_MOUNT
