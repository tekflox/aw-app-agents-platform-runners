"""Agent Config's "Share network" permission on the Runner execution path.

Bug confirmed live 2026-09-21 from inside a telegram-sonnet session running
under Agent Config "AW Full": `curl 127.0.0.1:9030` from inside the spawned
agent container gave "Connection refused" (not a timeout) — nothing of the
aw-workspace/aw-sandbox stack was reachable, even though the config's "Share
network" checkbox was ticked. `grep -n "share_network\\|network_mode"
execute.py` returned zero matches before this fix: unlike "docker" and
"tmp_access" (see test_permission_mounts.py — same bug, fixed
2026-08-13/2026-09-07), `permissions.get("share_network")` was never read
anywhere on this execution path, so the UI's checkbox was accepted and had
zero effect.

The fix resolves it into the docker-SDK `network_mode` kwarg, mirroring this
workspace's own docker-compose.yml pattern for joining aw-sandbox's netns:
`network_mode: "container:aw-sandbox"`. See test_share_network_live_mount.py
for the end-to-end check against a real container engine — this module only
covers the kwargs-building logic in isolation.

Run: python3 -m pytest -c /dev/null tests/test_share_network.py
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


def _kwargs(job: dict) -> dict:
    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(job)
    return kwargs


def _job(**overrides) -> dict:
    job = {"run_id": "r1", "cli": "claude", "prompt": "hi"}
    job.update(overrides)
    return job


def test_share_network_true_joins_aw_sandboxs_netns(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox")
    kwargs = _kwargs(_job(permissions={"share_network": True}))
    assert kwargs.get("network_mode") == "container:aw-sandbox"


def test_share_network_false_sets_no_network_mode(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    kwargs = _kwargs(_job(permissions={"share_network": False}))
    assert "network_mode" not in kwargs


def test_missing_share_network_key_sets_no_network_mode(tmp_path, monkeypatch):
    """Fail-CLOSED on absence, matching how workspace_access/docker/tmp_access
    already behave on this path — an unset key must never grant reach."""
    _setup(tmp_path, monkeypatch)
    kwargs = _kwargs(_job(permissions={"docker": True}))
    assert "network_mode" not in kwargs


def test_absent_permissions_dict_entirely_sets_no_network_mode(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    kwargs = _kwargs(_job())
    assert "network_mode" not in kwargs


def test_share_network_honours_the_sandbox_container_name_override(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox-custom")
    kwargs = _kwargs(_job(permissions={"share_network": True}))
    assert kwargs.get("network_mode") == "container:aw-sandbox-custom"


def test_share_network_true_takes_priority_over_container_network(tmp_path, monkeypatch):
    """A "container:<name>" NetworkMode makes the engine reuse aw-sandbox's
    whole network stack — it cannot also attach a separate user-defined
    network (AW_CONTAINER_NETWORK) on top of that. share_network must win,
    not silently combine into a kwargs dict the engine then rejects."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "CONTAINER_NETWORK", "some-bridge-network")
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox")
    kwargs = _kwargs(_job(permissions={"share_network": True}))
    assert kwargs.get("network_mode") == "container:aw-sandbox"
    assert "network" not in kwargs


def test_container_network_still_applies_when_share_network_is_off(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "CONTAINER_NETWORK", "some-bridge-network")
    kwargs = _kwargs(_job(permissions={"share_network": False}))
    assert kwargs.get("network") == "some-bridge-network"
    assert "network_mode" not in kwargs


class _FakeContainer:
    def __init__(self, name):
        self.name = name


class _FakeClient:
    """Stands in for the docker-SDK client _self_container(client) queries —
    just enough surface (containers.get by hostname, containers.list as the
    no-hostname fallback) for _self_container to resolve, without a real
    daemon."""

    def __init__(self, containers):
        self._containers = containers

    class _Containers:
        def __init__(self, outer):
            self._outer = outer

        def get(self, name):
            for c in self._outer._containers:
                if c.name == name:
                    return c
            raise LookupError(name)

        def list(self):
            return list(self._outer._containers)

    @property
    def containers(self):
        return self._Containers(self)


def test_share_network_on_a_hosted_byod_workspace_shares_with_our_own_container_not_aw_sandbox(tmp_path, monkeypatch):
    """The bug fixed 2026-09-21 (bug:apr-warm-dispatch-aw-sandbox-missing):
    on a hosted/BYOD workspace (e.g. crispal's aw-remote-host-workspace)
    there is no sibling container named "aw-sandbox" at all — the old
    hardcoded default made container creation fail outright with
    'no container with name or ID "aw-sandbox" found'. share_network must
    resolve to THIS runner's own container (_self_container) instead.

    Mutation check: reverting the fix (using SANDBOX_CONTAINER_NAME
    unconditionally) makes this assert "container:aw-remote-host-workspace"
    == "container:aw-sandbox", which fails — proving this test actually
    exercises the fixed code path."""
    ws = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox")
    # No "aw-sandbox" container anywhere on this engine — only our own,
    # matching the crispal topology confirmed live on the card.
    self_container = _FakeContainer("aw-remote-host-workspace")
    monkeypatch.setattr(execute_mod, "WORKSPACE_HOST_DIR", "")  # force the hostname-only lookup path
    monkeypatch.setattr(execute_mod, "_self_container", lambda client: self_container)
    client = _FakeClient([self_container])

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(
        _job(permissions={"share_network": True}), client)
    assert kwargs.get("network_mode") == "container:aw-remote-host-workspace"


def test_share_network_falls_back_to_sandbox_container_name_when_self_container_is_unknown(tmp_path, monkeypatch):
    """If _self_container can't identify us at all (should not normally
    happen), share_network still falls back to the old fixed name rather
    than crashing or silently disabling the permission."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox")
    monkeypatch.setattr(execute_mod, "_self_container", lambda client: None)
    client = _FakeClient([])

    _image, _argv, kwargs, _mcp = execute_mod._build_container_kwargs(
        _job(permissions={"share_network": True}), client)
    assert kwargs.get("network_mode") == "container:aw-sandbox"


def test_the_two_executors_agree_on_every_input(tmp_path, monkeypatch):
    """The property that actually matters, stated directly — mirrors
    test_permission_mounts.py's own test of this name for workspace_access.
    executor.py's runner-provider path forwards the raw permissions dict
    verbatim (see test_runner_llm_permissions.py); whatever this side does
    with "share_network" must not depend on which executor picked the run
    up."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(execute_mod, "SANDBOX_CONTAINER_NAME", "aw-sandbox")
    for perms in ({}, {"share_network": True}, {"share_network": False},
                  {"docker": True}, {"share_network": None}):
        expected = bool((perms or {}).get("share_network", False))
        joined = _kwargs(_job(permissions=perms)).get("network_mode") == "container:aw-sandbox"
        assert joined is expected, f"{perms!r}: runner={joined} expected={expected}"
