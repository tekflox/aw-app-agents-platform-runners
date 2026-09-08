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

import os
import socket
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402

WS_HOST = "/host/aw-workspace"
WS_BIND = "/opt/aw-workspace"


def _make_socket(path):
    """A real AF_UNIX socket at *path* — execute.py's docker-permission mount
    now requires `stat.S_ISSOCK`, not just Path.exists(), precisely because a
    plain file/dir at the well-known docker socket path is exactly the
    false-positive that made the permission cosmetic in production
    (2026-09-07)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


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
    sock_path = tmp_path / "docker.sock"
    sock = _make_socket(sock_path)
    try:
        monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(sock_path))

        vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
        assert vols[str(sock_path)] == {"bind": "/var/run/docker.sock", "mode": "rw"}
    finally:
        sock.close()


def test_docker_permission_off_leaves_the_socket_out(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sock_path = tmp_path / "docker.sock"
    sock = _make_socket(sock_path)
    try:
        monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(sock_path))

        vols = _volumes(_job(permissions={"workspace_access": True}))
        assert "/var/run/docker.sock" not in _binds(vols)
    finally:
        sock.close()


def test_docker_permission_with_a_plain_file_at_the_path_is_skipped(tmp_path, monkeypatch):
    """The actual false positive found live 2026-09-07: this workspace's own
    /var/run/docker.sock is a placeholder directory, not a socket — a bare
    Path.exists() check treats it as usable and 'successfully' resolves to a
    path that can never be bind-mounted as a working docker socket. A plain
    file reproduces the same false-positive shape and must be rejected too."""
    _setup(tmp_path, monkeypatch)
    not_a_socket = tmp_path / "docker.sock"
    not_a_socket.write_text("")
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(not_a_socket))

    vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert "/var/run/docker.sock" not in _binds(vols)


def test_docker_permission_with_a_directory_at_the_path_is_skipped(tmp_path, monkeypatch):
    """The literal shape of the live bug: a directory at the well-known
    default path, exactly what /var/run/docker.sock turned out to be here."""
    _setup(tmp_path, monkeypatch)
    a_directory = tmp_path / "docker.sock"
    a_directory.mkdir()
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(a_directory))

    vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert "/var/run/docker.sock" not in _binds(vols)


def test_docker_permission_with_no_socket_present_is_skipped(tmp_path, monkeypatch):
    # Granting the permission on a host without the socket must not turn a
    # missing path into a mount source the engine then refuses to start on.
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(tmp_path / "nope.sock"))

    vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert "/var/run/docker.sock" not in _binds(vols)


def test_docker_permission_with_no_socket_present_warns(tmp_path, monkeypatch, caplog):
    """The 'checkbox does nothing' bug (found 2026-09-07) must never be silent
    again — a granted-but-unmountable permission has to say so in the logs."""
    import logging
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(tmp_path / "nope.sock"))

    with caplog.at_level(logging.WARNING, logger="aw_apps.agents_platform_runners.execute"):
        _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert any("docker" in rec.message and "permission" in rec.message
               for rec in caplog.records)


def test_docker_permission_with_no_socket_configured_warns(tmp_path, monkeypatch, caplog):
    """DOCKER_SOCKET_PATH itself can resolve to None (both AW_DOCKER_SOCKET_PATH
    and AW_CONTAINER_SOCKET unset, no /var/run/docker.sock on this host) — must
    not raise on Path(None) and must still warn rather than mount nothing
    quietly."""
    import logging
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", None)

    with caplog.at_level(logging.WARNING, logger="aw_apps.agents_platform_runners.execute"):
        vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
    assert "/var/run/docker.sock" not in _binds(vols)
    assert any("docker" in rec.message and "permission" in rec.message
               for rec in caplog.records)


def test_docker_permission_mounts_the_daemons_path_not_ours(tmp_path, monkeypatch):
    """The 2026-09-08 root cause: the socket we can stat is a path in OUR mount
    namespace, but the daemon resolves a bind SOURCE in ITS own. On this
    podman-out-of-podman host /var/run/docker.sock and /run/podman.sock are both
    real from in here and neither exists on the outer host, so mounting either
    one handed the agent an empty DIRECTORY at /var/run/docker.sock."""
    _setup(tmp_path, monkeypatch)
    sock_path = tmp_path / "docker.sock"
    sock = _make_socket(sock_path)
    try:
        monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(sock_path))
        monkeypatch.setattr(execute_mod, "_docker_socket_bind_source",
                            lambda: "/run/podman/podman.sock")

        vols = _volumes(_job(permissions={"workspace_access": True, "docker": True}))
        assert vols["/run/podman/podman.sock"] == {"bind": "/var/run/docker.sock",
                                                   "mode": "rw"}
        assert str(sock_path) not in vols
    finally:
        sock.close()


# --- the bind SOURCE's own resolution ----------------------------------------


class _FakeContainer:
    def __init__(self, mounts):
        self.attrs = {"Mounts": mounts}


def _fake_docker_sdk(monkeypatch, container, expect_base_url=None):
    """A stand-in `docker` module whose DockerClient serves *container* for any
    lookup — enough for _self_container/_docker_socket_bind_source, which only
    ever call containers.get() and read .attrs."""
    import types

    class _Containers:
        def get(self, _name):
            if container is None:
                raise RuntimeError("no such container")
            return container

    class _Client:
        def __init__(self, base_url=None):
            if expect_base_url is not None:
                assert base_url == expect_base_url
            self.containers = _Containers()

    mod = types.ModuleType("docker")
    mod.DockerClient = _Client
    monkeypatch.setitem(sys.modules, "docker", mod)


def _reset_bind_source_cache(monkeypatch):
    monkeypatch.setattr(execute_mod, "_DOCKER_SOCKET_BIND_SOURCE", None)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_HOST_PATH", "")


def test_bind_source_env_override_wins(monkeypatch):
    """The explicit escape hatch, mirroring AW_WORKSPACE_HOST_DIR — no daemon
    call at all when the provisioner already told us the answer."""
    _reset_bind_source_cache(monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_HOST_PATH", "/somewhere/else.sock")
    _fake_docker_sdk(monkeypatch, _FakeContainer([]))

    assert execute_mod._docker_socket_bind_source() == "/somewhere/else.sock"


def test_bind_source_discovered_from_our_own_mount_table(monkeypatch):
    """The live shape: /var/run/docker.sock is a symlink to the destination the
    socket was actually bound at, and the daemon's side of that bind is the only
    valid mount source."""
    _reset_bind_source_cache(monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", "/run/podman.sock")
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/run/podman.sock")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", WS_HOST)
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", WS_BIND)
    monkeypatch.setenv("HOSTNAME", "5584662f4f57")
    _fake_docker_sdk(monkeypatch, _FakeContainer([
        {"Source": "/run/podman/podman.sock", "Destination": "/run/podman.sock"},
        {"Source": WS_HOST, "Destination": WS_BIND},
    ]), expect_base_url="unix:///run/podman.sock")

    assert execute_mod._docker_socket_bind_source() == "/run/podman/podman.sock"


def test_bind_source_follows_a_symlinked_socket_path(monkeypatch, tmp_path):
    """DOCKER_SOCKET_PATH can be /var/run/docker.sock while the mount table
    records /run/podman.sock — exactly this deployment. Matching on the literal
    string alone would miss it and silently fall back to our own path."""
    _reset_bind_source_cache(monkeypatch)
    real = tmp_path / "podman.sock"
    real.write_text("")
    link = tmp_path / "docker.sock"
    link.symlink_to(real)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", str(link))
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", str(real))
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "")
    monkeypatch.setenv("HOSTNAME", "5584662f4f57")
    _fake_docker_sdk(monkeypatch, _FakeContainer([
        {"Source": "/run/podman/podman.sock", "Destination": str(real)},
    ]))

    assert execute_mod._docker_socket_bind_source() == "/run/podman/podman.sock"


def test_bind_source_is_none_when_the_socket_is_not_bind_mounted_in(monkeypatch):
    """A plain (non-nested) docker host: the daemon shares our namespace, the
    socket was never bind-mounted in, and DOCKER_SOCKET_PATH is already a valid
    source. Must resolve to None so the caller keeps the old behaviour."""
    _reset_bind_source_cache(monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", "/var/run/docker.sock")
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/var/run/docker.sock")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "")
    monkeypatch.setenv("HOSTNAME", "5584662f4f57")
    _fake_docker_sdk(monkeypatch, _FakeContainer([
        {"Source": "/host/aw-workspace", "Destination": "/opt/aw-workspace"},
    ]))

    assert execute_mod._docker_socket_bind_source() is None


def test_bind_source_ignores_a_container_that_is_not_us(monkeypatch):
    """$HOSTNAME is the short container id unless someone passed --hostname —
    so a lookup that comes back WITHOUT the workspace mount we already know the
    host side of is somebody else's container, and its socket mount would be a
    wrong answer stated confidently."""
    _reset_bind_source_cache(monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", "/run/podman.sock")
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/run/podman.sock")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", WS_HOST)
    monkeypatch.setattr(execute_mod, "WORKSPACE_CONTAINER_DIR", WS_BIND)
    monkeypatch.setenv("HOSTNAME", "some-other-box")
    _fake_docker_sdk(monkeypatch, _FakeContainer([
        {"Source": "/wrong/host/path.sock", "Destination": "/run/podman.sock"},
    ]))

    assert execute_mod._docker_socket_bind_source() is None


def test_bind_source_survives_an_unreachable_daemon(monkeypatch):
    """A daemon that can't be reached must not raise out of the spawn path, and
    must not be written off for the life of the process either — only successes
    are cached."""
    import types
    _reset_bind_source_cache(monkeypatch)
    monkeypatch.setattr(execute_mod, "DOCKER_SOCKET_PATH", "/run/podman.sock")
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/run/podman.sock")

    mod = types.ModuleType("docker")

    def _boom(*a, **k):
        raise OSError("connection refused")

    mod.DockerClient = _boom
    monkeypatch.setitem(sys.modules, "docker", mod)

    assert execute_mod._docker_socket_bind_source() is None
    assert execute_mod._DOCKER_SOCKET_BIND_SOURCE is None


# --- DOCKER_SOCKET_PATH's own module-level fallback --------------------------


def test_docker_socket_path_defaults_to_container_socket_on_a_podman_host(monkeypatch):
    """The actual live bug: this workspace's /var/run/docker.sock is a
    placeholder DIRECTORY (not a socket) — the podman socket already resolved
    into AW_CONTAINER_SOCKET (used elsewhere in this file via
    docker_sdk.DockerClient, which podman's socket also speaks) is the one
    that actually works. Without this fallback, DOCKER_SOCKET_PATH stayed
    hardcoded to a path that exists-but-isn't-a-socket, and the 'docker'
    permission was cosmetic for every agent here — found live 2026-09-07 via
    a running agent's own mount table."""
    import importlib
    import types

    monkeypatch.delenv("AW_DOCKER_SOCKET_PATH", raising=False)
    monkeypatch.setenv("AW_CONTAINER_SOCKET", "/run/user/1001/podman/podman.sock")
    real_stat = os.stat
    monkeypatch.setattr(
        "os.stat",
        lambda p, *a, **k: types.SimpleNamespace(st_mode=stat.S_IFDIR)
        if p == "/var/run/docker.sock" else real_stat(p, *a, **k),
    )
    try:
        reloaded = importlib.reload(execute_mod)
        assert reloaded.DOCKER_SOCKET_PATH == "/run/user/1001/podman/podman.sock"
    finally:
        importlib.reload(execute_mod)  # restore the real module state for later tests


def test_docker_socket_path_prefers_the_real_docker_socket_when_present(monkeypatch):
    """A genuine docker host must keep working exactly as before — the podman
    fallback only kicks in when /var/run/docker.sock isn't a real socket."""
    import importlib
    import types

    monkeypatch.delenv("AW_DOCKER_SOCKET_PATH", raising=False)
    monkeypatch.setenv("AW_CONTAINER_SOCKET", "/run/user/1001/podman/podman.sock")
    monkeypatch.setattr(
        "os.stat",
        lambda p, *a, **k: types.SimpleNamespace(st_mode=stat.S_IFSOCK),
    )
    try:
        reloaded = importlib.reload(execute_mod)
        assert reloaded.DOCKER_SOCKET_PATH == "/var/run/docker.sock"
    finally:
        importlib.reload(execute_mod)  # restore the real module state for later tests


def test_docker_socket_path_honours_an_explicit_override(monkeypatch):
    import importlib

    monkeypatch.setenv("AW_DOCKER_SOCKET_PATH", "/custom/docker.sock")
    try:
        reloaded = importlib.reload(execute_mod)
        assert reloaded.DOCKER_SOCKET_PATH == "/custom/docker.sock"
    finally:
        importlib.reload(execute_mod)  # restore the real module state for later tests


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
