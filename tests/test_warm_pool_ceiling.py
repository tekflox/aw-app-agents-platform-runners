"""The warm pool had no size ceiling of any kind — `grep` for
`max_warm|MAX_WARM|evict|prune` across the app returned nothing relevant.

Each warm container holds a live `claude` process at ~145MB. A caller that
opens a NEW session per call therefore fills the entire TTL window with
containers, and the stateless `/v1/chat/completions` door does exactly that:
it never passes a session_id, so every call mints a fresh one
(mint_warm_session_id) and gets a fresh container. On 2026-10-01 that was
228 containers in 51 minutes at concurrency 4 — 37.2GB, a 62GB host at 0GB
free, swap exhausted, and every subsequent fork() failing. No OOM kill was
ever logged, because the kernel never killed anything; it simply had nothing
left to hand out.

The ceiling is a REFUSAL, not an eviction. Evicting an idle container to
make room means picking a victim among `running` containers, which is the
idle-vs-about-to-serve problem the pool cannot solve — and under the
workload that actually hits the ceiling, LRU degenerates anyway (each call's
container is immediately the next call's victim, so warm hit-rate goes to
~0 and you have paid eviction churn for cold-start performance). Refusing
and falling back to the cold path gets the same throughput with none of the
races, on a path that predates warm mode and auto-removes its containers.

Two boundaries matter most and are pinned hardest below: REUSE must never be
refused (a session that already has its container keeps it, however full the
pool is), and a 1-for-1 stale REPLACEMENT must still go through at the
ceiling — which it does naturally, because the stale container is renamed to
`-draining-` before the count happens.

Card: 3ec5bf3b-9510-8106-91f6-d9b31722daa4
Run: python3 -m pytest tests/test_warm_pool_ceiling.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import warm_pool  # noqa: E402

AGENT, SESSION, EPOCH = "agent-1", "sess-1", "epoch1"


class _FakeContainer:
    def __init__(self, name: str, status: str = "running", log=None):
        self.name = name
        self.status = status
        self.attrs = {}
        self._log = log if log is not None else []

    def rename(self, new_name: str) -> None:
        self._log.append(("rename", self.name, new_name))
        self.name = new_name

    def remove(self, force: bool = False) -> None:
        self._log.append(("remove", self.name, force))

    def reload(self) -> None:
        pass

    def exec_run(self, cmd):
        return 0, b""


class _FakeContainers:
    def __init__(self, pool: list, existing: set, log: list):
        self._pool = pool
        self._existing = existing
        self._log = log
        self.list_filters = None

    def get(self, name: str):
        if name not in self._existing:
            raise KeyError(name)
        # Return the POOLED object when there is one, so a rename through
        # get() is visible to the next list() — the engine renames the real
        # container, and a fake that handed out copies would make the
        # stale-replacement case pass or fail for the wrong reason.
        for c in self._pool:
            if c.name == name:
                return c
        return _FakeContainer(name, log=self._log)

    def list(self, all=False, filters=None):  # noqa: A002 - docker-py's own kwarg name
        self.list_filters = filters
        return list(self._pool)

    def run(self, image, **kwargs):
        self._log.append(("run", kwargs.get("name"), image))
        return _FakeContainer(kwargs["name"], log=self._log)


class _FakeClient:
    def __init__(self, pool=(), existing=(), log=None):
        self.log = log if log is not None else []
        pool = list(pool)
        for c in pool:  # pooled containers record into the client's own log
            c._log = self.log
        self.containers = _FakeContainers(pool, set(existing), self.log)


@pytest.fixture(autouse=True)
def _reset_ceiling():
    """configure() mutates module state — restore it so test order cannot
    leak a ceiling from one case into the next."""
    original = warm_pool._config_max_warm
    yield
    warm_pool._config_max_warm = original


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    monkeypatch.setattr(warm_pool, "_wait_ready", lambda _c, _n, timeout_s=10.0: None)
    monkeypatch.setattr(warm_pool, "drain", lambda _c, _n: None)


def _pool(n: int, status: str = "running", draining: bool = False) -> list:
    suffix = "-draining-1" if draining else ""
    return [_FakeContainer(f"aw-warm-other-{i}{suffix}", status=status) for i in range(n)]


def _get_or_create(client, **kw):
    return warm_pool.get_or_create(
        client=client, agent_id=AGENT, session_id=SESSION, epoch_hash=EPOCH,
        build_kwargs=lambda _name, _epoch: ("img", {}), **kw)


def _spawned(client) -> list:
    return [e for e in client.log if e[0] == "run"]


# --------------------------------------------------------------------------
# configure() — the ceiling has to survive a reinstall, so it comes from
# persisted app config, not a constant anyone has to redeploy to change.
# --------------------------------------------------------------------------

def test_default_ceiling_when_unconfigured():
    warm_pool.configure({})
    assert warm_pool.max_warm() == warm_pool.MAX_WARM_CONTAINERS


def test_ceiling_resolved_from_config():
    warm_pool.configure({"max_warm_containers": 7})
    assert warm_pool.max_warm() == 7


def test_ceiling_accepts_a_numeric_string():
    """A hand-edited config, or a schema that typed the field as text."""
    warm_pool.configure({"max_warm_containers": "12"})
    assert warm_pool.max_warm() == 12


def test_none_and_empty_fall_back_to_the_default():
    for raw in (None, "", "   "):
        warm_pool.configure({"max_warm_containers": raw})
        assert warm_pool.max_warm() == warm_pool.MAX_WARM_CONTAINERS


def test_zero_means_default_not_unlimited():
    """An unlimited pool is the exact condition the ceiling exists to stop,
    so there is deliberately no in-band way to ask for one — a typo'd 0 must
    not silently restore the incident."""
    warm_pool.configure({"max_warm_containers": 0})
    assert warm_pool.max_warm() == warm_pool.MAX_WARM_CONTAINERS


def test_negative_and_garbage_fall_back_to_the_default():
    for raw in (-1, "-5", "lots", "4.5", object()):
        warm_pool.configure({"max_warm_containers": raw})
        assert warm_pool.max_warm() == warm_pool.MAX_WARM_CONTAINERS, raw


def test_configuring_the_ceiling_does_not_disturb_the_warm_switch():
    assert warm_pool.configure({"warm_container": False, "max_warm_containers": 3}) is False
    assert warm_pool.max_warm() == 3


# --------------------------------------------------------------------------
# The spawn path
# --------------------------------------------------------------------------

def test_spawn_is_refused_at_the_ceiling():
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(3))

    with pytest.raises(warm_pool.WarmPoolFull) as excinfo:
        _get_or_create(client)

    assert not _spawned(client), "nothing may be spawned once the pool is full"
    # The message is what reaches the operator's log line on fallback.
    assert "3/3" in str(excinfo.value)


def test_spawn_is_allowed_one_below_the_ceiling():
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(2))

    assert _get_or_create(client) == warm_pool.warm_container_name(AGENT, SESSION)
    assert len(_spawned(client)) == 1


def test_overshooting_the_ceiling_still_refuses():
    """The ceiling is approximate under concurrency, so the pool CAN be
    found above it. `>=` rather than `==` is what stops that from reading as
    "room available" and running away again."""
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(9))

    with pytest.raises(warm_pool.WarmPoolFull):
        _get_or_create(client)


def test_warm_pool_full_is_catchable_as_its_own_type():
    """execute.py distinguishes this from every other dispatch failure —
    one falls back to cold, the others are hard spawn_errors."""
    assert issubclass(warm_pool.WarmPoolFull, RuntimeError)


# --------------------------------------------------------------------------
# What does NOT count toward the ceiling
# --------------------------------------------------------------------------

def test_draining_containers_do_not_count():
    """A drainer is on its way out and its replacement is usually the very
    spawn being checked. Counting them would wedge the pool shut for a full
    DRAIN_GRACE_S after any burst of recycling."""
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(2) + _pool(5, draining=True))

    assert _get_or_create(client)
    assert len(_spawned(client)) == 1


def test_stopped_containers_do_not_count():
    """A stopped container holds no CLI process — the thing being rationed."""
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(2) + _pool(9, status="exited"))

    assert _get_or_create(client)
    assert len(_spawned(client)) == 1


def test_only_warm_labeled_containers_are_counted():
    """Counting the ephemeral aw-runner-run-* containers would make a busy
    cold workload shut the warm pool down."""
    warm_pool.configure({"max_warm_containers": 3})
    client = _FakeClient(pool=_pool(1))

    _get_or_create(client)
    assert client.containers.list_filters == {"label": f"{warm_pool.WARM_LABEL}=1"}


def test_an_unlistable_socket_does_not_fail_closed(monkeypatch):
    """A transient engine error must not convert into a pool-wide cold
    fallback — "could not count" is not "full"."""
    warm_pool.configure({"max_warm_containers": 1})
    client = _FakeClient(pool=_pool(9))

    def _boom(all=False, filters=None):  # noqa: A002
        raise RuntimeError("socket gone")

    monkeypatch.setattr(client.containers, "list", _boom)
    assert _get_or_create(client)
    assert len(_spawned(client)) == 1


# --------------------------------------------------------------------------
# The two boundaries that must not regress
# --------------------------------------------------------------------------

def test_reuse_is_never_refused_at_the_ceiling(monkeypatch):
    """A session that already HAS its container keeps it, however full the
    pool is. Refusing here would be strictly worse than the bug: it would
    throw away a warm container that costs nothing to use, and send a
    perfectly good session cold.
    """
    warm_pool.configure({"max_warm_containers": 1})
    name = warm_pool.warm_container_name(AGENT, SESSION)
    monkeypatch.setattr(warm_pool, "_labels", lambda _c, _n: {warm_pool.EPOCH_LABEL: EPOCH})
    monkeypatch.setattr(warm_pool, "_is_running", lambda _c, _n: True)

    client = _FakeClient(pool=_pool(50), existing={name})

    assert _get_or_create(client) == name
    assert not _spawned(client), "reuse must not spawn, and must not even check the ceiling"


def test_stale_replacement_passes_at_exactly_one_below_the_ceiling(monkeypatch):
    """1-for-1 replacement of this session's own stale container. The stale
    one is renamed to `-draining-` BEFORE the count, so it stops counting
    and the replacement fits exactly — which is why the check sits after the
    rename rather than at the top of get_or_create.
    """
    warm_pool.configure({"max_warm_containers": 3})
    name = warm_pool.warm_container_name(AGENT, SESSION)
    # Epoch mismatch -> stale -> rename + respawn.
    monkeypatch.setattr(warm_pool, "_labels", lambda _c, _n: {warm_pool.EPOCH_LABEL: "old"})

    # Two other live containers + this session's stale one = 3 = the ceiling.
    stale = _FakeContainer(name)
    client = _FakeClient(pool=_pool(2) + [stale], existing={name})

    assert _get_or_create(client) == name
    assert [e for e in client.log if e[0] == "rename"], "the stale one must be renamed first"
    assert len(_spawned(client)) == 1


def test_a_new_session_at_the_ceiling_is_refused_while_replacement_is_not(monkeypatch):
    """The pair, side by side — same pool, same ceiling, different answer.
    This is the distinction the ordering of the check buys."""
    warm_pool.configure({"max_warm_containers": 3})
    name = warm_pool.warm_container_name(AGENT, SESSION)

    # No existing container for this session: a genuinely NEW one. Refused.
    monkeypatch.setattr(warm_pool, "_labels", lambda _c, _n: None)
    with pytest.raises(warm_pool.WarmPoolFull):
        _get_or_create(_FakeClient(pool=_pool(3)))

    # Same pool, but now this session has a stale container of its own.
    monkeypatch.setattr(warm_pool, "_labels", lambda _c, _n: {warm_pool.EPOCH_LABEL: "old"})
    client = _FakeClient(pool=_pool(2) + [_FakeContainer(name)], existing={name})
    assert _get_or_create(client) == name


def test_recycle_force_at_the_ceiling_still_respawns(monkeypatch):
    """recycle_session's whole purpose is handing a session a fresh CLI
    process. Being at the ceiling must not turn that into a failure — the
    container it removes is the room it needs."""
    warm_pool.configure({"max_warm_containers": 3})
    name = warm_pool.warm_container_name(AGENT, SESSION)
    monkeypatch.setattr(warm_pool, "_labels", lambda _c, _n: {warm_pool.EPOCH_LABEL: EPOCH})

    removed = _FakeContainer(name)
    client = _FakeClient(pool=_pool(2) + [removed], existing={name})
    # force-remove drops it from the engine, so it stops counting.
    client.containers._pool.remove(removed)

    assert _get_or_create(client, recycle="force") == name
    assert len(_spawned(client)) == 1
