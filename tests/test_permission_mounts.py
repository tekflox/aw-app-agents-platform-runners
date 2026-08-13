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


def test_missing_workspace_access_key_keeps_the_old_behaviour(tmp_path, monkeypatch):
    """Fail-OPEN on absence, unlike executor.py's fail-closed default.

    No Agent Config here ever had to set this key, so treating absence as
    false would un-mount the workspace from every agent at once. Absence
    keeps the tree and logs a warning; only an explicit false denies.
    """
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"github": False}))
    assert vols[WS_HOST] == {"bind": WS_BIND, "mode": "rw"}


def test_absent_permissions_dict_entirely_keeps_the_old_behaviour(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job())
    assert vols[WS_HOST] == {"bind": WS_BIND, "mode": "rw"}


def test_missing_key_warns_so_the_default_is_visible(tmp_path, monkeypatch, caplog):
    # A silent backwards-compatible default is how this class of bug hides;
    # the log line is what makes the eventual flip to fail-closed safe.
    _setup(tmp_path, monkeypatch)
    with caplog.at_level("WARNING"):
        _volumes(_job())
    assert any("workspace_access" in r.message for r in caplog.records)


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
    assert vols[f"{WS_HOST}/data/sandbox-tmp"] == {"bind": "/tmp", "mode": "rw"}


def test_tmp_access_off_leaves_tmp_alone(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    vols = _volumes(_job(permissions={"workspace_access": True}))
    assert "/tmp" not in _binds(vols)
