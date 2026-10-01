"""The warm sweep had to stop depending on dispatches happening.

`maybe_reap()` runs the sweep, but only when a turn is dispatched — and the
incident this exists for was one where dispatches were the thing failing.
228 leaked containers had filled the host; every new `/execute` died at
`fork()` with cli.error/spawn_error; and the one mechanism that would have
collected them only ran on the code path that could no longer run. The
collector was reachable exactly when it was not needed.

`start_reap_loop()` is the sweep that runs anyway. Its correctness is almost
entirely about what it survives, so that is what these tests are: it must
not start twice, must not start without a socket to talk to, must not die on
an exception, and must not be tied to warm mode being on — leftovers still
need collecting after warm is switched off, and nothing else will ever pick
them up.

No test here sleeps. The loop body takes its interval as an argument and
`time.sleep` is stubbed, because a test that waits out REAP_INTERVAL_S is a
test nobody runs.

Card: 3ec5bf3b-9510-8106-91f6-d9b31722daa4
Run: python3 -m pytest tests/test_warm_reap_loop.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402
from agents_platform_runners_app import warm_pool  # noqa: E402


class _RecordedThread:
    """Captures the thread instead of running it — the loop is infinite."""

    started: list = []

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target, self.args, self.name, self.daemon = target, args, name, daemon

    def start(self):
        _RecordedThread.started.append(self)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """start_reap_loop is idempotent through MODULE state, so every test has
    to reset it or the second test in the file silently asserts nothing."""
    _RecordedThread.started = []
    monkeypatch.setattr(execute_mod, "_reap_loop_started", False)
    monkeypatch.setattr(execute_mod.threading, "Thread", _RecordedThread)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/run/fake.sock")


# --------------------------------------------------------------------------
# Starting it
# --------------------------------------------------------------------------

def test_the_loop_thread_is_started():
    assert execute_mod.start_reap_loop() is True

    assert len(_RecordedThread.started) == 1
    thread = _RecordedThread.started[0]
    assert thread.name == "warm-reap-loop"
    assert thread.daemon is True, "a non-daemon sweep would block process exit"


def test_starting_twice_does_not_stack_threads():
    """activate() can run more than once in a process (reinstall, config
    reload). Two sweep threads would double every podman call forever."""
    assert execute_mod.start_reap_loop() is True
    assert execute_mod.start_reap_loop() is False
    assert execute_mod.start_reap_loop() is False

    assert len(_RecordedThread.started) == 1


def test_no_container_socket_means_no_loop(monkeypatch):
    """Mirrors reap_dead_warm_containers' own guard: with no socket there is
    nothing to sweep, and a loop that rebuilds a client against "unix://"
    every 600s is pure log noise."""
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "")

    assert execute_mod.start_reap_loop() is False
    assert _RecordedThread.started == []


def test_the_socket_guard_does_not_consume_the_idempotency_flag(monkeypatch):
    """A socket-less early return that still flipped the started flag would
    permanently disable the loop for a process that got its socket later."""
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "")
    execute_mod.start_reap_loop()

    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/run/fake.sock")
    assert execute_mod.start_reap_loop() is True


def test_the_default_interval_is_the_shared_reap_interval():
    execute_mod.start_reap_loop()
    assert _RecordedThread.started[0].args == (warm_pool.REAP_INTERVAL_S,)


def test_the_interval_is_overridable():
    execute_mod.start_reap_loop(interval_s=5)
    assert _RecordedThread.started[0].args == (5,)


# --------------------------------------------------------------------------
# The loop body — what it must survive
# --------------------------------------------------------------------------

class _Stop(Exception):
    """Breaks the infinite loop from inside the stubbed sleep."""


def _run_loop(monkeypatch, *, iterations: int, reap):
    """Run `_reap_loop` for a bounded number of iterations."""
    ticks = {"n": 0}

    def _sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] > iterations:
            raise _Stop()

    monkeypatch.setattr(execute_mod.time, "sleep", _sleep)
    monkeypatch.setattr(warm_pool, "reap", reap)

    import docker as docker_sdk
    monkeypatch.setattr(docker_sdk, "DockerClient", lambda **kw: object())

    with pytest.raises(_Stop):
        execute_mod._reap_loop(0.01)
    return ticks["n"]


def test_the_loop_sweeps_once_per_interval(monkeypatch):
    swept = []
    _run_loop(monkeypatch, iterations=3, reap=lambda client: swept.append(client))
    assert len(swept) == 3


def test_the_loop_sleeps_before_its_first_sweep(monkeypatch):
    """activate() already fires a one-shot boot sweep on its own thread;
    sweeping again immediately would just double it during startup, which is
    the busiest moment the podman socket has."""
    order = []

    def _sleep(_seconds):
        order.append("sleep")
        if order.count("sleep") > 1:
            raise _Stop()

    monkeypatch.setattr(execute_mod.time, "sleep", _sleep)
    monkeypatch.setattr(warm_pool, "reap", lambda client: order.append("reap"))

    import docker as docker_sdk
    monkeypatch.setattr(docker_sdk, "DockerClient", lambda **kw: object())

    with pytest.raises(_Stop):
        execute_mod._reap_loop(0.01)

    assert order == ["sleep", "reap", "sleep"]


def test_a_failing_sweep_does_not_kill_the_loop(monkeypatch):
    """The whole value of this thread is being alive on the bad day. An
    absent, slow or restarting socket must cost one iteration, not the
    collector."""
    calls = {"n": 0}

    def _always_raises(_client):
        calls["n"] += 1
        raise RuntimeError("socket gone")

    _run_loop(monkeypatch, iterations=4, reap=_always_raises)
    assert calls["n"] == 4, "the loop stopped at the first failure"


def test_the_loop_recovers_after_a_failure(monkeypatch):
    results = []

    def _flaky(_client):
        results.append(len(results))
        if len(results) == 1:
            raise RuntimeError("transient")

    _run_loop(monkeypatch, iterations=3, reap=_flaky)
    assert len(results) == 3


def test_each_iteration_builds_a_fresh_client(monkeypatch):
    """A client captured once would keep failing identically, and silently,
    after the socket behind it went away."""
    built = []
    swept = []

    monkeypatch.setattr(execute_mod.time, "sleep",
                        lambda _s: None if len(swept) < 3 else (_ for _ in ()).throw(_Stop()))
    monkeypatch.setattr(warm_pool, "reap", lambda client: swept.append(client))

    import docker as docker_sdk

    def _client(**kw):
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr(docker_sdk, "DockerClient", _client)

    with pytest.raises(_Stop):
        execute_mod._reap_loop(0.01)

    assert len(built) == 3
    assert len(set(map(id, built))) == 3, "the same client object was reused"


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_activate_starts_the_loop_outside_the_warm_on_gate():
    """Static check on plugin.py: the call must not sit under `if warm_on`.

    Turning warm OFF stops new containers being created; it does not collect
    the ones already running. If the only periodic collector is gated on
    warm being on, switching it off to mitigate an incident strands every
    container that incident created — which is the opposite of mitigation.
    """
    source = (ROOT / "agents_platform_runners_app" / "plugin.py").read_text()

    call_line = next(i for i, line in enumerate(source.splitlines())
                     if "start_reap_loop()" in line)
    gate_line = next(i for i, line in enumerate(source.splitlines())
                     if line.strip() == "if warm_on:")

    call_indent = len(source.splitlines()[call_line]) - len(source.splitlines()[call_line].lstrip())
    gate_indent = len(source.splitlines()[gate_line]) - len(source.splitlines()[gate_line].lstrip())

    assert call_line > gate_line
    assert call_indent <= gate_indent, (
        "start_reap_loop() is indented under `if warm_on:` — the periodic "
        "sweep must run regardless of whether warm mode is enabled")
