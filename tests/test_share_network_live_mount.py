"""End-to-end check that a REAL spawned agent container with the "Share
network" permission actually lands inside aw-sandbox's network namespace —
not the unit-level coverage test_share_network.py already has.

Same class of bug, same reasoning for a live test, as
test_docker_socket_live_mount.py (card bug:ap-mt-runner-agent-config-not-
forwarded, 2026-09-07): a fix can be correct on disk, pass every
monkeypatched unit test, and still be cosmetic in a running deployment
because the already-running process never re-imported the new code. The only
way to catch THAT is to talk to the real container engine this process is
actually configured against, spawn a real throwaway container, and check
what it can actually reach — not what the kwargs dict says it should be able
to reach.

Confirmed live 2026-09-21 (before this fix): `curl 127.0.0.1:9030` from
inside a spawned agent container running under Agent Config "AW Full" (which
grants "Share network") gave "Connection refused" — this workspace's own
:9030 (see docker-compose.yml's `AW_PORT: "9030"`) was unreachable because
the container was never joined to aw-sandbox's netns in the first place.

Skips cleanly wherever AW_CONTAINER_SOCKET isn't a real connectable socket,
the docker Python package/daemon isn't reachable, or there is no "aw-sandbox"
container to join (every normal CI run — same posture as
test_docker_socket_live_mount.py). Run: .venv/aw/bin/python -m pytest
tests/test_share_network_live_mount.py
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


def _sandbox_container_joinable() -> bool:
    """Whether "aw-sandbox" (or its AW_SANDBOX_CONTAINER_NAME override)
    actually exists on this engine — a share_network grant against a name
    that doesn't resolve fails container creation outright, so this needs
    its own reachability gate distinct from _container_engine_reachable()."""
    if not _container_engine_reachable():
        return False
    try:
        import docker as docker_sdk
        client = docker_sdk.DockerClient(base_url="unix://" + execute_mod.CONTAINER_SOCKET)
        client.containers.get(execute_mod.SANDBOX_CONTAINER_NAME)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_container_joinable(),
    reason="no reachable AW_CONTAINER_SOCKET or no joinable "
           f"{execute_mod.SANDBOX_CONTAINER_NAME!r} container — this only runs "
           "against a real live deployment, not a bare CI checkout",
)


def test_share_network_resolves_to_the_container_mode_kwarg():
    """Cheapest possible live check, no spawn needed: whatever THIS process
    currently has loaded for _build_container_kwargs must actually set
    network_mode for a share_network=True job."""
    job = {"run_id": "share-network-live-check", "cli": "claude", "prompt": "hi",
           "permissions": {"share_network": True}}
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    assert kwargs.get("network_mode") == f"container:{execute_mod.SANDBOX_CONTAINER_NAME}", (
        f"share_network=True produced network_mode={kwargs.get('network_mode')!r} — "
        "the permission is not being forwarded into the container spec"
    )


def test_a_freshly_spawned_agent_container_reaches_aw_sandboxs_loopback():
    """The actual regression: build the SAME kwargs production's warm/cold
    spawn paths build for a share_network=True job, spin up a real,
    throwaway container with them, and check what it can ACTUALLY reach on
    127.0.0.1 — not a mock, the live container engine's own answer. Mirrors
    test_docker_socket_live_mount.py's own live-spawn shape."""
    import docker as docker_sdk

    client = docker_sdk.DockerClient(base_url="unix://" + execute_mod.CONTAINER_SOCKET)

    job = {"run_id": "share-network-live-check", "cli": "claude", "prompt": "hi",
           "permissions": {"share_network": True}}
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    network_mode = kwargs.get("network_mode")
    assert network_mode, "no network_mode in the kwargs a share_network=True job produces"

    image = "alpine:latest"
    try:
        client.images.pull(image)
    except Exception as e:  # noqa: BLE001 — no network in this environment
        pytest.skip(f"could not pull {image} to run the live check: {e}")

    # busybox's `nc` is part of alpine's base image — no extra install needed.
    # Port 9030 is this workspace's own AW_PORT (docker-compose.yml), the same
    # port the bug report's live "Connection refused" was reproduced against.
    output = client.containers.run(
        image, command=["sh", "-c", "nc -z -w2 127.0.0.1 9030 && echo OPEN || echo CLOSED"],
        network_mode=network_mode,
        remove=True,
    )
    result = output.decode().strip()
    assert result == "OPEN", (
        f"a freshly spawned agent container with network_mode={network_mode!r} "
        f"could not reach 127.0.0.1:9030 (result={result!r}) — the share_network "
        f"permission is live-broken even though the fix is on disk. This is the "
        f"exact 2026-09-07-shaped incident the docker permission hit: code "
        f"fixed, unit tests green, production still serving the old (isolated) "
        f"network — the already-running process needs a restart to pick this "
        f"module up."
    )
