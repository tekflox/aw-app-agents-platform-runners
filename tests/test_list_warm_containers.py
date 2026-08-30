"""list_warm_containers's read path (warm_pool.list_containers): which warm
containers are alive right now, and whose. Every container spawned before
``CLI_LABEL`` existed has no ``aw.cli`` — the inference fallback (guessing
from the wrapper entrypoint each spawn path bakes in) has to be covered
alongside the plain label read, or the tool ships reporting ``cli: null``
for every container alive at deploy time.

The other thing this file guards: a failed listing must never look like an
empty pool. Those are different answers a caller cannot otherwise tell
apart.

Run: .venv/aw/bin/python -m pytest tests/test_list_warm_containers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402


class _FakeContainer:
    def __init__(self, *, id: str, name: str, status: str = "running",
                 labels: dict | None = None, created: str = "2026-08-30T00:00:00Z",
                 entrypoint: list[str] | None = None):
        self.id = id
        self.name = name
        self.status = status
        # Shape returned by containers.list(all=True) — Labels top-level,
        # no Config yet (that needs a full inspect).
        self.attrs = {"Labels": labels or {}, "Created": created}
        self._entrypoint = entrypoint or []
        self.reloaded = False

    def reload(self) -> None:
        self.reloaded = True
        # Full-inspect shape: Labels move under Config.
        self.attrs = {
            "Created": self.attrs.get("Created"),
            "Config": {"Entrypoint": self._entrypoint, "Labels": self.attrs.get("Labels", {})},
        }


class _FakeContainers:
    def __init__(self, containers, list_raises: bool = False):
        self._containers = containers
        self._list_raises = list_raises
        self.list_filters = None

    def list(self, all=False, filters=None):  # noqa: A002 - docker-py's own kwarg name
        if self._list_raises:
            raise RuntimeError("socket gone")
        self.list_filters = filters
        return list(self._containers)


class _FakeClient:
    def __init__(self, containers, list_raises: bool = False):
        self.containers = _FakeContainers(containers, list_raises)


# --------------------------------------------------------------------------
# cli source: label read vs entrypoint inference
# --------------------------------------------------------------------------

def test_label_read_is_reported_as_such():
    c = _FakeContainer(id="a" * 64, name="aw-warm-agent1-sess1",
                       labels={warm_pool.CLI_LABEL: "claude",
                               warm_pool.AGENT_ID_LABEL: "agent1",
                               warm_pool.SESSION_ID_LABEL: "sess1",
                               warm_pool.EPOCH_LABEL: "epoch1"})
    out = warm_pool.list_containers(_FakeClient([c]))
    assert out == [{
        "container_id": "a" * 12,
        "name": "aw-warm-agent1-sess1",
        "status": "running",
        "session_id": "sess1",
        "agent_id": "agent1",
        "cli": "claude",
        "cli_source": "label",
        "epoch": "epoch1",
        "created": "2026-08-30T00:00:00Z",
        "draining": False,
    }]
    assert not c.reloaded, "the label already answered the question — no need to inspect further"


def test_codex_is_inferred_from_its_wrapper_entrypoint_when_label_is_missing():
    c = _FakeContainer(id="b" * 64, name="aw-warm-agent2-sess2",
                       labels={warm_pool.AGENT_ID_LABEL: "agent2",
                               warm_pool.SESSION_ID_LABEL: "sess2"},
                       entrypoint=["/usr/local/bin/aw-warm-wrapper-codex"])
    out = warm_pool.list_containers(_FakeClient([c]))
    assert len(out) == 1
    assert out[0]["cli"] == "codex"
    assert out[0]["cli_source"] == "inferred"
    assert c.reloaded, "no label present — must have inspected the container to guess"


def test_claude_is_inferred_from_its_wrapper_entrypoint_when_label_is_missing():
    c = _FakeContainer(id="c" * 64, name="aw-warm-agent3-sess3",
                       entrypoint=["/usr/local/bin/aw-warm-wrapper"])
    out = warm_pool.list_containers(_FakeClient([c]))
    assert out[0]["cli"] == "claude"
    assert out[0]["cli_source"] == "inferred"


def test_unrecognized_entrypoint_infers_to_none_not_a_crash():
    c = _FakeContainer(id="d" * 64, name="aw-warm-agent4-sess4",
                       entrypoint=["/bin/sh"])
    out = warm_pool.list_containers(_FakeClient([c]))
    assert out[0]["cli"] is None
    assert out[0]["cli_source"] == "inferred"


# --------------------------------------------------------------------------
# draining
# --------------------------------------------------------------------------

def test_draining_container_excluded_by_default():
    live = _FakeContainer(id="e" * 64, name="aw-warm-agent5-sess5")
    draining = _FakeContainer(id="f" * 64, name="aw-warm-agent5-sess5-draining-1234")
    out = warm_pool.list_containers(_FakeClient([live, draining]))
    assert [row["name"] for row in out] == ["aw-warm-agent5-sess5"]


def test_draining_container_included_on_request():
    live = _FakeContainer(id="e" * 64, name="aw-warm-agent5-sess5")
    draining = _FakeContainer(id="f" * 64, name="aw-warm-agent5-sess5-draining-1234")
    out = warm_pool.list_containers(_FakeClient([live, draining]), include_draining=True)
    names = {row["name"]: row["draining"] for row in out}
    assert names == {"aw-warm-agent5-sess5": False,
                      "aw-warm-agent5-sess5-draining-1234": True}


# --------------------------------------------------------------------------
# failure mode — the one that matters most
# --------------------------------------------------------------------------

def test_listing_failure_raises_instead_of_returning_an_empty_list():
    """An empty list means 'no warm containers'. A caller that gets [] back
    from a socket that just died cannot tell the difference — so this must
    raise, unlike reap()'s own listing call, which is fire-and-forget."""
    client = _FakeClient([], list_raises=True)
    with pytest.raises(RuntimeError):
        warm_pool.list_containers(client)


def test_only_warm_labeled_containers_are_requested():
    client = _FakeClient([])
    warm_pool.list_containers(client)
    assert client.containers.list_filters == {"label": f"{warm_pool.WARM_LABEL}=1"}
