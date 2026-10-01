"""The warm pool's TTL was enforced ONLY from inside each container, by
aw-warm-wrapper's own watcher subshell. warm_pool.WARM_TTL_S said so in its
own comment: "never polled or enforced from out here". So when that watcher
did not fire, nothing ever collected the container — reap() took only
STOPPED containers and `-draining-` ones past DRAIN_GRACE_S, and its
docstring conceded that a running idle container "is indistinguishable" from
one about to serve.

A `running` warm container with a dead watcher was therefore immortal. On
2026-10-01, 228 of them (up to 3h old against a 30-min TTL) held 37.2GB on a
62GB host, took it to 0GB free, and made every subsequent fork() fail —
145 documents terminally failed on cli.error/spawn_error, with no OOM kill
ever logged because the kernel never killed anything.

reap()'s third stage is the backstop. It does NOT resolve the
indistinguishability — that sentence is still true — it sidesteps it by
deciding on AGE, which the host can observe directly, and by acting
GRACEFULLY: condemn == rename to `-draining-<ts>` + touch the drain flag,
exactly what get_or_create already does to a stale container. No stop(), no
kill(), no remove(). A turn genuinely in flight on an over-TTL container
still runs to completion, because the wrapper's 15s poll loop closes stdin
only once the current turn is done. Force-removal stays where it was: the
pre-existing wedged-drainer stage, which collects the condemned container
DRAIN_GRACE_S later if it is still running — and one that is, is wedged.

The tests below pin all three halves of that: the age decision (including
both `Created` wire shapes and the never-condemn-on-a-guess rule), the
graceful mechanics, and the end-to-end composition with the stage that
already existed.

Card: 3ec5bf3b-9510-8106-91f6-d9b31722daa4
Run: python3 -m pytest tests/test_warm_ttl_backstop.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402

DRAIN_FLAG = ["touch", "/home/ubuntu/.aw-warm/drain"]

# Comfortably past the backstop's own condemn threshold, expressed in terms
# of the constants rather than a literal so this keeps meaning "over TTL"
# whatever WARM_TTL_S is tuned to.
OVER_TTL_S = warm_pool.WARM_TTL_S + warm_pool.WARM_TTL_SLACK_S + 60


class _FakeContainer:
    def __init__(self, name: str, status: str = "running", created=None,
                 labels=None, rename_raises: bool = False,
                 exec_raises: bool = False):
        self.name = name
        self.status = status
        self.attrs = {}
        if created is not None:
            self.attrs["Created"] = created
        if labels is not None:
            self.attrs["Labels"] = labels
        self.removed = False
        self.removed_force = None
        self.renamed_to = None
        self.exec_runs = []
        self._rename_raises = rename_raises
        self._exec_raises = exec_raises

    def remove(self, force: bool = False):
        self.removed = True
        self.removed_force = force

    def rename(self, new_name: str):
        if self._rename_raises:
            raise RuntimeError("podman said no")
        self.renamed_to = new_name
        self.name = new_name

    def exec_run(self, cmd):
        self.exec_runs.append(cmd)
        if self._exec_raises:
            raise RuntimeError("exec refused")
        return 0, b""


class _FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, all=False, filters=None):  # noqa: A002 - docker-py's own kwarg name
        return list(self._containers)


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


def _aged(name: str, age_s: float, **kw) -> _FakeContainer:
    """A warm container created `age_s` ago, `Created` as the int unix epoch
    that `containers.list()`'s abbreviated attrs actually carry."""
    return _FakeContainer(name, created=int(time.time() - age_s), **kw)


# --------------------------------------------------------------------------
# The age decision
# --------------------------------------------------------------------------

def test_over_ttl_running_container_is_condemned_not_removed():
    """The headline case: the 3h-old survivor of a dead in-container watcher.

    Condemned gracefully — renamed and drain-flagged — and explicitly NOT
    removed. Removing a running container here would break the drain-not-kill
    invariant that warm_pool.drain()'s docstring makes permanent.
    """
    stuck = _aged("aw-warm-a-1", OVER_TTL_S)
    client = _FakeClient([stuck])

    assert warm_pool.reap(client) == 0, "condemning removes nothing — that is the point"

    assert stuck.renamed_to is not None, "an over-TTL container must be condemned"
    assert stuck.renamed_to.startswith("aw-warm-a-1-draining-")
    assert DRAIN_FLAG in stuck.exec_runs, "condemnation must touch the drain flag"
    assert not stuck.removed, "drain-not-kill: the backstop never removes a running container"


def test_rename_suffix_is_a_parseable_timestamp_the_next_stage_can_read():
    """The condemn and the force-remove stages are coupled through this
    suffix alone — a format the wedged-drainer parser cannot read would
    leave the container condemned forever and collected never."""
    stuck = _aged("aw-warm-a-1", OVER_TTL_S)
    warm_pool.reap(_FakeClient([stuck]))

    stamp = int(stuck.renamed_to.rsplit("-draining-", 1)[1])
    assert abs(stamp - int(time.time())) < 60


def test_container_just_inside_ttl_plus_slack_is_left_alone():
    """The slack exists to give the in-container watcher first go. Condemning
    at WARM_TTL_S itself would race it over every container in the pool."""
    young = _aged("aw-warm-a-1", warm_pool.WARM_TTL_S + warm_pool.WARM_TTL_SLACK_S - 60)
    client = _FakeClient([young])

    assert warm_pool.reap(client) == 0
    assert young.renamed_to is None
    assert young.exec_runs == []


def test_container_past_ttl_but_inside_the_slack_is_left_alone():
    past_ttl_only = _aged("aw-warm-a-1", warm_pool.WARM_TTL_S + 10)
    client = _FakeClient([past_ttl_only])

    assert warm_pool.reap(client) == 0
    assert past_ttl_only.renamed_to is None


def test_unparseable_created_is_never_condemned():
    """Never condemn on a guess. An unreadable Created means "unknown age",
    and the backstop's whole safety argument rests on age being a fact."""
    for created in ("not-a-date", "", True, {"nested": "thing"}, [1, 2]):
        mystery = _FakeContainer("aw-warm-a-1", created=created)
        assert warm_pool.reap(_FakeClient([mystery])) == 0
        assert mystery.renamed_to is None, f"condemned on Created={created!r}"


def test_missing_created_is_never_condemned():
    no_created = _FakeContainer("aw-warm-a-1")
    assert warm_pool.reap(_FakeClient([no_created])) == 0
    assert no_created.renamed_to is None


# --------------------------------------------------------------------------
# Both `Created` wire shapes — the SDK returns a different one depending on
# whether the container came from list() or from a full inspect, and the
# backstop reads containers straight off list().
# --------------------------------------------------------------------------

def test_created_as_int_epoch_is_understood():
    """What `containers.list()`'s abbreviated attrs actually carry."""
    stuck = _FakeContainer("aw-warm-a-1", created=int(time.time() - OVER_TTL_S))
    assert warm_pool.reap(_FakeClient([stuck])) == 0
    assert stuck.renamed_to is not None


def test_created_as_iso8601_string_is_understood():
    """What a full inspect carries — nanosecond precision and a bare "Z",
    which datetime.fromisoformat only learned to take in 3.11."""
    created = (datetime.now(timezone.utc) - timedelta(seconds=OVER_TTL_S))
    iso_nanos = created.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created.microsecond:06d}123Z"

    stuck = _FakeContainer("aw-warm-a-1", created=iso_nanos)
    assert warm_pool.reap(_FakeClient([stuck])) == 0
    assert stuck.renamed_to is not None, f"ISO8601 shape {iso_nanos!r} was not parsed"


def test_iso8601_offset_and_microsecond_shapes_are_understood():
    created = datetime.now(timezone.utc) - timedelta(seconds=OVER_TTL_S)
    for text in (created.isoformat(),                      # +00:00 offset
                 created.strftime("%Y-%m-%dT%H:%M:%SZ"),   # no fraction
                 created.replace(tzinfo=None).isoformat()):  # naive -> UTC
        stuck = _FakeContainer("aw-warm-a-1", created=text)
        assert warm_pool.reap(_FakeClient([stuck])) == 0
        assert stuck.renamed_to is not None, f"{text!r} was not parsed"


def test_a_young_iso8601_container_is_not_condemned():
    """Guards the sign of the ISO arithmetic — a parser that returned a
    negative or absolute-value age would condemn the whole pool."""
    created = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    young = _FakeContainer("aw-warm-a-1", created=created)
    assert warm_pool.reap(_FakeClient([young])) == 0
    assert young.renamed_to is None


# --------------------------------------------------------------------------
# What the backstop must NOT touch
# --------------------------------------------------------------------------

def test_already_draining_container_is_not_condemned_again():
    """A drainer is already on its way out; renaming it again would stack
    `-draining-` suffixes and reset the grace clock on every single sweep,
    so a wedged drainer would never reach the force-remove stage at all."""
    draining = _aged(f"aw-warm-a-1-draining-{int(time.time()) - 30}", OVER_TTL_S)
    client = _FakeClient([draining])

    assert warm_pool.reap(client) == 0
    assert draining.renamed_to is None
    assert "-draining-" in draining.name
    assert draining.name.count("-draining-") == 1


def test_stopped_over_ttl_container_is_removed_by_the_existing_stage():
    """Stopped is stopped — it goes to the original stopped-container stage,
    not through a pointless condemnation of a container with no process."""
    dead = _aged("aw-warm-a-1", OVER_TTL_S, status="exited")
    client = _FakeClient([dead])

    assert warm_pool.reap(client) == 1
    assert dead.removed
    assert dead.renamed_to is None


def test_young_and_old_containers_in_one_sweep_are_handled_independently():
    young = _aged("aw-warm-a-1", 60)
    old = _aged("aw-warm-b-2", OVER_TTL_S)
    client = _FakeClient([young, old])

    warm_pool.reap(client)

    assert young.renamed_to is None
    assert old.renamed_to is not None


# --------------------------------------------------------------------------
# Mechanics: locking, labels, and failure modes
# --------------------------------------------------------------------------

def test_condemnation_is_taken_under_the_sessions_own_lock():
    """get_or_create holds this same lock while it decides to reuse a
    container. Condemning outside it is how a rename lands between a
    dispatch choosing a name and dispatch_turn looking it up."""
    held = []
    real_session_lock = warm_pool._session_lock

    def _tracking_lock(agent_id, session_id):
        held.append((agent_id, session_id))
        return real_session_lock(agent_id, session_id)

    stuck = _aged("aw-warm-a-1", OVER_TTL_S, labels={
        warm_pool.AGENT_ID_LABEL: "a", warm_pool.SESSION_ID_LABEL: "1",
    })

    original = warm_pool._session_lock
    warm_pool._session_lock = _tracking_lock
    try:
        warm_pool.reap(_FakeClient([stuck]))
    finally:
        warm_pool._session_lock = original

    assert held == [("a", "1")], "condemnation must resolve and take the session lock"
    assert stuck.renamed_to is not None


def test_labels_nested_under_config_are_also_read():
    """A full inspect nests Labels under Config — the same two shapes
    list_containers() already documents."""
    stuck = _aged("aw-warm-a-1", OVER_TTL_S)
    stuck.attrs = {"Created": stuck.attrs["Created"], "Config": {"Labels": {
        warm_pool.AGENT_ID_LABEL: "a", warm_pool.SESSION_ID_LABEL: "1",
    }}}

    held = []
    original = warm_pool._session_lock
    warm_pool._session_lock = lambda a, s: (held.append((a, s)), original(a, s))[1]
    try:
        warm_pool.reap(_FakeClient([stuck]))
    finally:
        warm_pool._session_lock = original

    assert held == [("a", "1")]


def test_unlabelled_container_is_still_condemned_just_unlocked():
    """Warm containers predating the labels still leak, and are still the
    ones most likely to have a stale wrapper. Skipping them would exempt
    exactly the worst population."""
    stuck = _aged("aw-warm-a-1", OVER_TTL_S, labels={})
    assert warm_pool.reap(_FakeClient([stuck])) == 0
    assert stuck.renamed_to is not None


def test_failed_drain_flag_still_leaves_the_container_renamed():
    """Renamed-but-unflagged is a recoverable state: the stable name is free
    and the wedged-drainer stage still collects it. Rolling the rename back
    would strand it under the live name instead."""
    stuck = _aged("aw-warm-a-1", OVER_TTL_S, exec_raises=True)
    assert warm_pool.reap(_FakeClient([stuck])) == 0
    assert stuck.renamed_to is not None


def test_failed_rename_does_not_abort_the_sweep():
    stubborn = _aged("aw-warm-a-1", OVER_TTL_S, rename_raises=True)
    collectable = _FakeContainer("aw-warm-a-2", status="exited")
    client = _FakeClient([stubborn, collectable])

    assert warm_pool.reap(client) == 1
    assert collectable.removed


def test_thresholds_are_parameterizable():
    """reap() takes ttl_s/slack_s as kwargs so a caller (and these tests) can
    exercise the stage without waiting out the real 30 minutes."""
    stuck = _aged("aw-warm-a-1", 100)
    assert warm_pool.reap(_FakeClient([stuck]), ttl_s=10, slack_s=5) == 0
    assert stuck.renamed_to is not None

    young = _aged("aw-warm-b-2", 100)
    assert warm_pool.reap(_FakeClient([young]), ttl_s=1000, slack_s=5) == 0
    assert young.renamed_to is None


# --------------------------------------------------------------------------
# The composition — condemn, then the pre-existing stage collects. This is
# the whole design: neither stage force-removes a healthy container, and
# together they bound a stuck container's life.
# --------------------------------------------------------------------------

def test_condemned_container_is_force_removed_once_it_outlasts_the_grace():
    """End to end through reap() itself, across the grace window.

    Sweep 1 condemns the over-TTL survivor. It keeps running (its wrapper is
    wedged — it ignored the drain flag, which is exactly why it was over TTL
    in the first place). Sweep 2, a grace period later, is the pre-existing
    wedged-drainer stage doing its original job on it.
    """
    stuck = _aged("aw-warm-a-1", OVER_TTL_S)
    client = _FakeClient([stuck])

    assert warm_pool.reap(client) == 0
    condemned_name = stuck.renamed_to
    assert condemned_name is not None
    assert not stuck.removed, "still inside its grace — a live turn may be finishing"

    # Still inside the grace window: left alone, as a draining container must be.
    assert warm_pool.reap(client) == 0
    assert not stuck.removed

    # ...and once it has outlasted the grace, the stage that always existed
    # collects it. Shortened via the kwarg rather than by waiting an hour.
    assert warm_pool.reap(client, drain_grace_s=0) == 1
    assert stuck.removed
    assert stuck.removed_force is True


def test_a_container_that_obeys_the_drain_is_collected_as_a_corpse():
    """The good case, for contrast: condemned, the wrapper exits on its own,
    and the ORIGINAL stopped-container stage collects it — no force-remove
    anywhere in the sequence."""
    stuck = _aged("aw-warm-a-1", OVER_TTL_S)
    client = _FakeClient([stuck])

    assert warm_pool.reap(client) == 0
    assert DRAIN_FLAG in stuck.exec_runs

    stuck.status = "exited"  # the wrapper noticed the flag and left
    assert warm_pool.reap(client) == 1
    assert stuck.removed


# --------------------------------------------------------------------------
# The other half of the race mitigation, in get_or_create rather than reap().
#
# The backstop renames containers out from under their stable name. A
# dispatch that has already taken a name from get_or_create and is on its
# way to dispatch_turn's containers.get() would then fail loudly. The window
# is narrowed from both ends: reap() condemns under the session's own lock
# (above), and get_or_create refuses to HAND OUT a container the backstop is
# about to come for — so no dispatch ever newly chooses one.
# --------------------------------------------------------------------------

class _GocContainers:
    def __init__(self, pool, log):
        self._pool = pool
        self._log = log

    def get(self, name: str):
        for c in self._pool:
            if c.name == name:
                return c
        raise KeyError(name)

    def list(self, all=False, filters=None):  # noqa: A002 - docker-py's own kwarg name
        return list(self._pool)

    def run(self, image, **kwargs):
        self._log.append(("run", kwargs.get("name")))
        c = _FakeContainer(kwargs["name"])
        self._pool.append(c)
        return c


class _GocClient:
    def __init__(self, pool):
        self.log = []
        self.containers = _GocContainers(list(pool), self.log)


def _goc(client, **kw):
    return warm_pool.get_or_create(
        client=client, agent_id="a", session_id="1", epoch_hash="e1",
        build_kwargs=lambda _name, _epoch: ("img", {}), **kw)


def _goc_fixture(monkeypatch, age_s):
    """A running, epoch-MATCHING warm container of the given age — i.e. the
    exact state get_or_create would otherwise reuse without a question."""
    monkeypatch.setattr(warm_pool, "_labels",
                        lambda _c, _n: {warm_pool.EPOCH_LABEL: "e1"})
    monkeypatch.setattr(warm_pool, "_is_running", lambda _c, _n: True)
    monkeypatch.setattr(warm_pool, "_wait_ready", lambda _c, _n, timeout_s=10.0: None)
    monkeypatch.setattr(warm_pool, "drain", lambda _c, _n: None)
    monkeypatch.setattr(warm_pool, "_check_ceiling", lambda _c, _n: None)
    name = warm_pool.warm_container_name("a", "1")
    existing = _aged(name, age_s) if age_s is not None else _FakeContainer(name)
    return _GocClient([existing]), existing, name


def test_get_or_create_refuses_to_reuse_an_over_ttl_container(monkeypatch):
    """Without this, the dispatch that reuses it and the sweep that condemns
    it are racing over the same container."""
    client, existing, name = _goc_fixture(monkeypatch, warm_pool.WARM_TTL_S + 60)

    assert _goc(client) == name
    assert existing.renamed_to is not None, "the over-TTL one must be drained, not handed out"
    assert client.log == [("run", name)], "and a fresh container spawned under the same name"


def test_get_or_create_reuses_a_container_inside_its_ttl(monkeypatch):
    """The ordinary case, and the one that would be destroyed by an
    off-by-one or a sign error above — reuse is the entire value of the
    pool, so this is the invariant the refusal must not overreach into."""
    client, existing, name = _goc_fixture(monkeypatch, 60)

    assert _goc(client) == name
    assert existing.renamed_to is None
    assert client.log == [], "an in-TTL container must be reused, not respawned"


def test_get_or_create_reuses_when_the_age_is_unknown(monkeypatch):
    """Same never-guess rule as the backstop: an unreadable Created must not
    silently turn every reuse into a respawn."""
    client, existing, name = _goc_fixture(monkeypatch, None)

    assert _goc(client) == name
    assert existing.renamed_to is None
    assert client.log == []
