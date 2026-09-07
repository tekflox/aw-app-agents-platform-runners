"""End-to-end check that a REAL spawned agent container gets a REAL docker
socket — not the unit-level coverage test_permission_mounts.py already has.

Card bug:ap-mt-runner-agent-config-not-forwarded (2026-09-07): the "docker"
permission was cosmetic in production for hours after the fix (82ea802,
091bbee, released as v0.122.0) was on disk and test_permission_mounts.py's
20 monkeypatched unit tests were green. The unit tests import this module
fresh in a throwaway pytest process, so they can only prove the FIX'S LOGIC
is correct — they cannot catch "the already-running production process
never re-imported it", which is exactly what happened live. The only way to
catch that class of bug is to talk to the REAL container engine this
process is actually configured against and inspect a REAL container's REAL
mount, using whatever code THIS process currently has loaded — so run this
directly against a live deployment (e.g. on the aw-workspace host itself,
against apps_root()/agents-platform-runners) to get a genuine answer to
"is the docker permission working right now", not just "is the code right".

Skips cleanly wherever AW_CONTAINER_SOCKET isn't a real connectable socket
(every normal CI run — same reachability gate as test_permission_mounts.py's
own DOCKER_SOCKET_PATH resolution) or the docker Python package/daemon isn't
reachable — same posture as test_app_lifespan_order.py's Postgres-reachable
tests. Run: .venv/aw/bin/python -m pytest tests/test_docker_socket_live_mount.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


def _container_engine_reachable() -> bool:
    if not execute_mod._is_usable_socket(execute_mod.CONTAINER_SOCKET):
        return False
    try:
        import docker as docker_sdk
        client = docker_sdk.DockerClient(base_url="unix://" + execute_mod.CONTAINER_SOCKET)
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_engine_reachable(),
    reason="no reachable AW_CONTAINER_SOCKET — this only runs against a real "
           "container engine (a live workspace, not a bare CI checkout)",
)


def test_docker_socket_path_currently_resolves_to_a_real_socket():
    """Cheapest possible live check, no container spawn needed: whatever THIS
    process currently has loaded for DOCKER_SOCKET_PATH must itself be a real
    AF_UNIX socket right now — a plain file or directory here is exactly the
    2026-09-07 incident (an empty placeholder directory at
    /var/run/docker.sock silently accepted as "found")."""
    assert execute_mod._is_usable_socket(execute_mod.DOCKER_SOCKET_PATH), (
        f"DOCKER_SOCKET_PATH={execute_mod.DOCKER_SOCKET_PATH!r} is not a real, "
        "connectable socket in THIS process right now — the docker permission "
        "would silently no-op for any agent this process spawns"
    )


def test_a_freshly_spawned_agent_container_gets_a_real_docker_socket_mount():
    """The actual regression: build the SAME volumes dict production's
    warm/cold spawn paths build for a docker=True job, spin up a real,
    throwaway container with them, and inspect the REAL resulting mount —
    not a mock, not a re-import, the live container engine's own answer."""
    import docker as docker_sdk

    client = docker_sdk.DockerClient(base_url="unix://" + execute_mod.CONTAINER_SOCKET)

    job = {"run_id": "docker-socket-live-check", "cli": "claude", "prompt": "hi",
           "permissions": {"docker": True}}
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    volumes = kwargs.get("volumes") or {}

    docker_sock_binds = [src for src, spec in volumes.items()
                         if spec.get("bind") == "/var/run/docker.sock"]
    assert docker_sock_binds, (
        "docker=True produced no /var/run/docker.sock bind at all — the "
        "permission is not being forwarded into the container spec"
    )
    host_source = docker_sock_binds[0]

    image = "alpine:latest"
    try:
        client.images.pull(image)
    except Exception as e:  # noqa: BLE001 — no network in this environment
        pytest.skip(f"could not pull {image} to run the live check: {e}")

    output = client.containers.run(
        image, command=["stat", "-c", "%F", "/var/run/docker.sock"],
        volumes={host_source: {"bind": "/var/run/docker.sock", "mode": "rw"}},
        remove=True,
    )
    kind = output.decode().strip()
    assert kind == "socket", (
        f"a freshly spawned agent container sees /var/run/docker.sock as a "
        f"{kind!r}, not a socket — the docker permission is live-broken even "
        f"though the fix is on disk (host_source={host_source!r}). This is "
        f"the exact 2026-09-07 incident: code fixed, unit tests green, "
        f"production still serving the old mount."
    )
    assert execute_mod._is_usable_socket(host_source), (
        f"host_source={host_source!r} (what got bind-mounted in) is not a "
        "real socket from this process's own view either"
    )
