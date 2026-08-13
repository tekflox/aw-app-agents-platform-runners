"""Agent Config permissions on the Runner execution path.

agents-platform's executor.py resolves ``workspace_access`` / ``docker`` /
``tmp_access`` into mounts for its own docker executor, and forwards the raw
permissions dict to this runner for the runner-provider case. Until
2026-08-13 this side read only ``github`` from that dict, so the other three
were accepted in the UI and silently did nothing — and ``workspace_access``
in particular was inverted: the workspace tree was mounted rw unconditionally,
including for the crispal-* agents whose config opts out and whose system
prompts assert they have no workspace filesystem.

See test_git_creds_mount.py for the ``github`` permission's own coverage.

Run: python3 -m pytest -c /dev/null tests/test_permission_mounts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

WS_HOST = "/host/aw-workspace"
WS_BIND = "/opt/aw-workspace"


def _setup(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execute_mod, "REAL_HOME", str(tmp_path / "home-unused"))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOME_HOST_DIR", "")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", WS_HOST)
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", str(ws))
    monkeypatch.setattr(execute_mod, "_sync_home_creds_into_workspace",
                        lambda *a, **k: None)
    return ws


def _volumes(job: dict) -> dict:
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return kwargs["volumes"]


def _job(**overrides) -> dict:
    job = {"run_id": "r1", "cli": "claude", "prompt": "hi"}
    job.update(overrides)
    return job


def _binds(vols: dict) -> set[str]:
    return {v["bind"] for v in vols.values()}


# --- workspace_access --------------------------------------------------------


def test_workspace_access_true_mounts_the_tree_rw(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": True}))
    assert vols[WS_HOST] == {"bind": WS_BIND, "mode": "rw"}


def test_workspace_access_false_withholds_the_tree(tmp_path, monkeypatch):
    """The regression this module exists for.

    An explicit opt-out was ignored, so an agent told (by its own prompt)
    that it has no workspace filesystem was handed the whole tree rw.
    """
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": False}))
    assert WS_HOST not in vols
    assert WS_BIND not in _binds(vols)


def test_workspace_access_false_also_withholds_the_workspace_cli(tmp_path, monkeypatch):
    """Denying the tree but leaving the CLI would hand back the same reach.

    aw-workspace-cli drives this workspace's own API (apps, folders,
    remote-hosts) — it is not a neutral binary.
    """
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": False}))
    assert "/usr/local/bin/aw-workspace-cli" not in _binds(vols)


def test_workspace_access_true_still_mounts_the_workspace_cli(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": True}))
    assert "/usr/local/bin/aw-workspace-cli" in _binds(vols)


def test_missing_workspace_access_key_denies(tmp_path, monkeypatch):
    """Fail-CLOSED on absence, matching executor.py exactly.

    Shipped fail-open for a few hours on 2026-08-13, as a hedge against
    configs that might not carry the key. All six on the live tenant do, so
    the hedge protected nothing and left the same Agent Config meaning two
    different things depending on which executor ran it.
    """
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"github": False}))
    assert WS_HOST not in vols
    assert WS_BIND not in _binds(vols)


def test_absent_permissions_dict_entirely_denies(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job())
    assert WS_HOST not in vols
    assert WS_BIND not in _binds(vols)


def test_the_two_executors_agree_on_every_input(tmp_path, monkeypatch):
    """The property that actually matters, stated directly.

    executor.py computes bool(permissions.get("workspace_access", False)).
    Anything this side does that differs turns a permission into a
    coincidence of which executor picked the run up.
    """
    _setup(tmp_path, monkeypatch)
    for perms in ({}, {"workspace_access": True}, {"workspace_access": False},
                  {"github": True}, {"workspace_access": None}):
        expected = bool((perms or {}).get("workspace_access", False))
        mounted = WS_HOST in _volumes(_job(permissions=perms))
        assert mounted is expected, f"{perms!r}: runner={mounted} executor={expected}"


# --- docker ------------------------------------------------------------------


def test_docker_permission_mounts_the_socket(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(sock))

    vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert vols[str(sock)] == {"bind": "/var/run/docker.sock", "mode": "rw"}


def test_docker_permission_off_leaves_the_socket_out(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(sock))

    vols = _volumes(_job(permissions={"workspace_access": True}))
    assert "/var/run/docker.sock" not in _binds(vols)


def test_docker_permission_with_no_socket_present_is_skipped(tmp_path, monkeypatch):
    # Granting the permission on a host without the socket must not turn a
    # missing path into a mount source the engine then refuses to start on.
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(tmp_path / "nope.sock"))

    vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert "/var/run/docker.sock" not in _binds(vols)


# --- tmp_access --------------------------------------------------------------


def test_tmp_access_mounts_the_shared_sandbox_tmp(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": True, "tmp_access": True}))
    rel = ".aw-workspace/data/agents-platform-runners/sandbox-tmp"
    assert vols[f"{WS_HOST}/{rel}"] == {"bind": "/tmp", "mode": "rw"}


def test_tmp_access_source_exists_and_is_writable_by_the_run_uid(tmp_path, monkeypatch):
    """This bind REPLACES the image's 1777 /tmp. If the source does not
    already exist, podman creates it root:root 0755 and the container — which
    runs as the workspace uid — cannot write its own scratch:
        EACCES: permission denied, mkdir '/tmp/claude-1001'
    The run then lands green with that line as its entire output."""
    import os
    _setup(tmp_path, monkeypatch)
    _volumes(_job(permissions={"workspace_access": True, "tmp_access": True}))

    src = (Path(execute_mod.WORKSPACE_CONTAINER_DIR)
           / ".aw-workspace" / "data" / "agents-platform-runners" / "sandbox-tmp")
    assert src.is_dir(), "the mount source must be created BEFORE podman sees the path"
    assert oct(os.stat(src).st_mode & 0o777) == "0o777"


def test_tmp_access_off_leaves_tmp_alone(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": True}))
    assert "/tmp" not in _binds(vols)
