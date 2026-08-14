"""Warm containers are spawned with ``remove=False`` — they have to be, since
one outlives the run that created it — so nothing ever cleaned up the ones
that died. Drained and TTL-expired containers just accumulated: 49 aw-warm-*
on the podman host on 2026-08-14, 33 of them ``-draining-``, all long stopped,
on a BYOD box whose disk has run near-full before.

reap() is that missing collector. What it must NOT do matters as much: an
idle-but-running warm container is indistinguishable from one about to get
the next message, and removing it would put the pool back to cold-per-turn.

Run: .venv/aw/bin/python -m pytest tests/test_warm_reap.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402


class _FakeContainer:
    def __init__(self, name: str, status: str = "running", remove_raises: bool = False):
        self.name = name
        self.status = status
        self.removed = False
        self.removed_force = None
        self._remove_raises = remove_raises

    def remove(self, force: bool = False):
        if self._remove_raises:
            raise RuntimeError("podman said no")
        self.removed = True
        self.removed_force = force


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


def _draining(name: str, age_s: int, status: str = "running") -> _FakeContainer:
    return _FakeContainer(f"{name}-draining-{int(time.time()) - age_s}", status=status)


# --------------------------------------------------------------------------
# What gets collected
# --------------------------------------------------------------------------

def test_stopped_warm_containers_are_removed():
    dead = _FakeContainer("aw-warm-a-1", status="exited")
    client = _FakeClient([dead])
    assert warm_pool.reap(client) == 1
    assert dead.removed


def test_running_warm_container_is_never_touched():
    """This is the pool. An idle one is just a session between messages."""
    live = _FakeContainer("aw-warm-a-1", status="running")
    client = _FakeClient([live])
    assert warm_pool.reap(client) == 0
    assert not live.removed


def test_wedged_drainer_is_force_removed():
    stuck = _draining("aw-warm-a-1", age_s=warm_pool.DRAIN_GRACE_S + 60)
    client = _FakeClient([stuck])
    assert warm_pool.reap(client) == 1
    assert stuck.removed
    assert stuck.removed_force is True


def test_recent_drainer_is_left_to_finish_its_turn():
    """Draining is uncapped by design — a turn still in flight must be allowed
    to end on its own, which is the whole reason drain() is a flag file and
    not a kill."""
    draining = _draining("aw-warm-a-1", age_s=60)
    client = _FakeClient([draining])
    assert warm_pool.reap(client) == 0
    assert not draining.removed


def test_stopped_drainer_is_removed_regardless_of_age():
    done = _draining("aw-warm-a-1", age_s=30, status="exited")
    client = _FakeClient([done])
    assert warm_pool.reap(client) == 1
    assert done.removed


def test_only_warm_labeled_containers_are_listed():
    """A stray filter change here would hand reap() the ephemeral
    aw-runner-run-* containers — or every container on the host."""
    client = _FakeClient([])
    warm_pool.reap(client)
    assert client.containers.list_filters == {"label": f"{warm_pool.WARM_LABEL}=1"}


# --------------------------------------------------------------------------
# Failure modes — a sweep runs on the dispatch path and must never escalate
# --------------------------------------------------------------------------

def test_unlistable_socket_is_not_an_error():
    assert warm_pool.reap(_FakeClient([], list_raises=True)) == 0


def test_one_failed_removal_does_not_abort_the_sweep():
    stubborn = _FakeContainer("aw-warm-a-1", status="exited", remove_raises=True)
    collectable = _FakeContainer("aw-warm-a-2", status="exited")
    client = _FakeClient([stubborn, collectable])
    assert warm_pool.reap(client) == 1
    assert collectable.removed


def test_unparseable_draining_suffix_is_skipped_not_crashed():
    weird = _FakeContainer("aw-warm-a-1-draining-notanumber", status="running")
    client = _FakeClient([weird])
    assert warm_pool.reap(client) == 0
    assert not weird.removed


# --------------------------------------------------------------------------
# Throttling — maybe_reap() sits on the turn's critical path
# --------------------------------------------------------------------------

def test_maybe_reap_runs_once_then_throttles(monkeypatch):
    calls = []
    monkeypatch.setattr(warm_pool, "reap", lambda client: calls.append(client))
    # Threads would make the assertion racy; run the body inline instead.
    monkeypatch.setattr(warm_pool.threading, "Thread",
                        lambda target, name=None, daemon=None: _InlineThread(target))
    monkeypatch.setattr(warm_pool, "_last_reap", 0.0)

    client = _FakeClient([])
    warm_pool.maybe_reap(client)
    warm_pool.maybe_reap(client)
    assert len(calls) == 1, "a sweep on every dispatch would hit the socket far too often"


def test_maybe_reap_runs_again_after_the_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(warm_pool, "reap", lambda client: calls.append(client))
    monkeypatch.setattr(warm_pool.threading, "Thread",
                        lambda target, name=None, daemon=None: _InlineThread(target))
    monkeypatch.setattr(warm_pool, "_last_reap", 0.0)

    client = _FakeClient([])
    warm_pool.maybe_reap(client)
    monkeypatch.setattr(warm_pool, "_last_reap",
                        time.monotonic() - warm_pool.REAP_INTERVAL_S - 1)
    warm_pool.maybe_reap(client)
    assert len(calls) == 2


class _InlineThread:
    def __init__(self, target):
        self._target = target

    def start(self):
        self._target()
