"""Regression test for the monitor-run raw-bash execution path
(Kanban card ap-mt:monitor-run-dead-workspace-mount).

agents-platform-multitenant's monitor_run.py used to spawn `docker run`
itself, against ITS OWN bare-metal daemon, where the workspace tree does not
exist. The fix routes it through this app's /execute instead — same auth,
container spawn and Redis Stream plumbing as a CLI-agent job, but with
`raw_command` set it starts `bash -lc "<command>"` in the shell image,
identity-mapped onto the SAME "/opt/aw-workspace" mount a real agent run
gets, so a `cwd` means the same thing to both.

Run: .venv/aw/bin/python -m pytest tests/test_execute_raw_command.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app.routes import build_routes  # noqa: E402


def test_build_raw_kwargs_starts_bash_under_the_identity_mapped_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/home/aw-remote-host/aw-workspace")
    # No mirrored git creds under here — isolates this test's exact-volumes
    # assertion from whatever aw-app-git happens to have mirrored on the
    # real host the tests run on (see the git-creds tests below).
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setattr(execute_mod, "REGISTRY", "ghcr.io")
    monkeypatch.setattr(execute_mod, "IMAGE_PREFIX", "fredericowu/aw-sandbox-agent-cli")
    monkeypatch.setattr(execute_mod, "DEFAULT_TAG", "latest")

    image, argv, kwargs = execute_mod._build_raw_kwargs({
        "run_id": "abc123",
        "raw_command": "git rev-parse HEAD",
        "cwd": "repos/agents-platform-multitenant",
    })

    assert image == "ghcr.io/fredericowu/aw-sandbox-agent-cli-shell:latest"
    assert argv == ["bash", "-lc", "git rev-parse HEAD"]
    assert kwargs["working_dir"] == "/opt/aw-workspace/repos/agents-platform-multitenant"
    # The SAME bind target every CLI-agent spawn's workspace mount uses
    # (_build_container_kwargs) — this is what makes a monitor run's cwd
    # resolve identically to a real agent run's.
    assert kwargs["volumes"] == {
        "/home/aw-remote-host/aw-workspace": {"bind": "/opt/aw-workspace", "mode": "rw"}
    }
    # NOT remove=True: the raw_command branch in _run_job_blocking waits for
    # the container then fetches its logs in one shot, which needs the
    # container to still exist after exit — podman's follow-mode log API was
    # found (2026-08-30) to return empty immediately for a container with no
    # buffered stdout yet, racing engine-side auto-removal for a fast/silent
    # command (`sleep 2; exit 7` reproduced it live; a command with output at
    # attach time did not). Cleanup happens explicitly instead.
    assert kwargs["remove"] is False


def test_build_raw_kwargs_defaults_to_the_workspace_root_with_no_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/home/aw-remote-host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))

    _, _, kwargs = execute_mod._build_raw_kwargs({"run_id": "abc123", "raw_command": "ls"})

    assert kwargs["working_dir"] == "/opt/aw-workspace"


def test_build_raw_kwargs_mounts_git_creds_read_only_when_mirrored(tmp_path, monkeypatch):
    """A monitor run has no Agent Config, hence no `permissions.github` to
    gate on (Kanban card agents-platform:monitor-run-no-git-creds-mount) —
    it mounts the same aw-app-git-mirrored creds the CLI-agent path does,
    unconditionally, since it already receives the whole workspace rw."""
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/home/aw-remote-host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))
    git_dir = tmp_path / execute_mod.GIT_CREDS_REL
    (git_dir / "config-gh").mkdir(parents=True)
    (git_dir / "config-gh" / "hosts.yml").write_text("github.com:\n  oauth_token: abc\n")
    (git_dir / "gitconfig").write_text("[user]\n\tname = octocat\n")

    _, _, kwargs = execute_mod._build_raw_kwargs({"run_id": "abc123", "raw_command": "gh auth status"})

    gh_src = f"/home/aw-remote-host/aw-workspace/{execute_mod.GIT_CREDS_REL}/config-gh"
    gitconfig_src = f"/home/aw-remote-host/aw-workspace/{execute_mod.GIT_CREDS_REL}/gitconfig"
    assert kwargs["volumes"][gh_src] == {"bind": "/home/ubuntu/.config/gh", "mode": "ro"}
    assert kwargs["volumes"][gitconfig_src] == {"bind": "/home/ubuntu/.gitconfig", "mode": "ro"}


def test_build_raw_kwargs_mounts_no_git_creds_when_never_mirrored(tmp_path, monkeypatch):
    """aw-app-git never logged in (or was uninstalled) — must not crash,
    must not mount a nonexistent source."""
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/home/aw-remote-host/aw-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(tmp_path))

    _, _, kwargs = execute_mod._build_raw_kwargs({"run_id": "abc123", "raw_command": "echo hi"})

    assert kwargs["volumes"] == {
        "/home/aw-remote-host/aw-workspace": {"bind": "/opt/aw-workspace", "mode": "rw"}
    }


def test_build_container_kwargs_branches_to_raw_before_touching_cli_specs(monkeypatch):
    """A raw_command job must never reach the CLI-agent branch (no CLI_SPECS
    lookup, no credential mounts) — asserted by NOT setting up anything a
    CLI-agent build would need (WORKSPACE_HOME_HOST_DIR, REAL_HOME, etc.)."""
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "/home/aw-remote-host/aw-workspace")

    image, argv, kwargs, mcp_path = execute_mod._build_container_kwargs({
        "run_id": "abc123", "raw_command": "echo hi",
    })

    assert argv == ["bash", "-lc", "echo hi"]
    assert mcp_path is None
    assert "user" in kwargs and "environment" in kwargs


def test_execute_route_forwards_raw_command_and_cwd(monkeypatch):
    captured = {}

    def fake_start_job(job, redis_url):
        captured.update(job)

    monkeypatch.setattr(execute_mod, "start_job", fake_start_job)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "unix:///fake.sock")

    client = TestClient(build_routes({
        "execute_secret": "s3cr3t",
        "shared_redis_url": "redis://example.test:6379/0",
    }))
    resp = client.post(
        "/execute",
        headers={"x-runner-secret": "s3cr3t"},
        json={
            "raw_command": "git rev-parse HEAD",
            "cwd": "repos/agents-platform-multitenant",
            "timeout_seconds": 60,
        },
    )
    assert resp.status_code == 200
    assert captured.get("raw_command") == "git rev-parse HEAD"
    assert captured.get("cwd") == "repos/agents-platform-multitenant"
    assert captured.get("timeout_seconds") == 60
