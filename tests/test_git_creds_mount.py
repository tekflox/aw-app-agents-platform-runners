"""Regression coverage for the Agent Config "GitHub / Git" permission
(agents-platform-multitenant's executor.py forwards
permissions.get("github") through RunnerLLM's dispatch payload; routes.py's
job dict literal forwards it here — see test_execute_job_forwarding.py's
contract test) — mounts gh/git creds mirrored by aw-app-git's
gh_auth.py._sync_creds_to_data_dir() into the spawned container, read-only.

Run: .venv/aw/bin/python -m pytest tests/test_git_creds_mount.py
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


def _setup(tmp_path, monkeypatch, *, with_creds: bool):
    ws = tmp_path / "ws"
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home-unused"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")  # -> claude fallback, irrelevant here
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))
    # No .claude creds present -> the fallback's own resync/mount is a no-op;
    # keeps assertions below scoped to the git-specific mounts only.
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace", lambda *a, **k: None)

    if with_creds:
        git_dir = ws / execute_mod.GIT_CREDS_REL
        (git_dir / "config-gh").mkdir(parents=True)
        (git_dir / "config-gh" / "hosts.yml").write_text("github.com:\n  oauth_token: abc\n")
        (git_dir / "gitconfig").write_text("[user]\n\tname = octocat\n")
    return ws


def _volumes_for(job: dict) -> dict:
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return kwargs["volumes"]


def test_github_permission_on_mounts_creds_read_only(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, with_creds=True)

    vols = _volumes_for(_base_job(permissions={"github": True}))

    gh_src = f"/host/aw-workspace/{execute_mod.GIT_CREDS_REL}/config-gh"
    gitconfig_src = f"/host/aw-workspace/{execute_mod.GIT_CREDS_REL}/gitconfig"
    assert vols[gh_src] == {"bind": "/home/ubuntu/.config/gh", "mode": "ro"}
    assert vols[gitconfig_src] == {"bind": "/home/ubuntu/.gitconfig", "mode": "ro"}


def test_github_permission_off_mounts_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, with_creds=True)

    vols = _volumes_for(_base_job(permissions={"github": False}))

    assert not any(v["bind"] == "/home/ubuntu/.config/gh" for v in vols.values())
    assert not any(v["bind"] == "/home/ubuntu/.gitconfig" for v in vols.values())


def test_no_permissions_key_mounts_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, with_creds=True)

    vols = _volumes_for(_base_job())  # no "permissions" key at all

    assert not any(v["bind"] == "/home/ubuntu/.config/gh" for v in vols.values())


def test_github_permission_on_but_no_creds_yet_mounts_nothing(tmp_path, monkeypatch):
    """aw-app-git never logged in (or was uninstalled) — must not crash, must
    not mount a nonexistent source."""
    _setup(tmp_path, monkeypatch, with_creds=False)

    vols = _volumes_for(_base_job(permissions={"github": True}))

    assert not any(v["bind"] == "/home/ubuntu/.config/gh" for v in vols.values())
    assert not any(v["bind"] == "/home/ubuntu/.gitconfig" for v in vols.values())
