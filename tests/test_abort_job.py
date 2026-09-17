"""`execute.abort_job` — the kill half of the /abort endpoint.

Kanban bug:abort-does-not-propagate-to-runner-backed-run: a Telegram /abort
marked the Run row cancelled on agents-platform-multitenant and then killed
nothing, because for a Runner-backed run there is no container on AP-MT's own
docker host — the agent lives here and kept running to completion.

This bug's whole nature is a SILENT no-op, so every test below is written to
fail when its specific line of the fix is reverted (see the mutation notes on
each group). A green suite with the fix removed would prove nothing.

The `docker` SDK is stubbed into sys.modules rather than imported: this app's
release workflow installs only `pytest jsonschema fastapi httpx uvicorn`, so a
test that needs the real SDK passes on a dev box and fails the release gate.
Same reasoning as tests/test_warm_raw_prompt_forwarded.py's own note.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import execute as execute_mod  # noqa: E402


class _FakeRedis:
    """Enough of redis-py for the registry + the done sentinel. `store` can be
    shared between two instances to stand in for two workers on one Redis."""

    def __init__(self, store: dict | None = None) -> None:
        self.store = store if store is not None else {}
        self.published: list[dict] = []
        self.closed = False

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def xadd(self, key, fields, **_kw):
        self.published.append(dict(fields))

    def expire(self, *_a, **_kw):
        pass

    def close(self):
        self.closed = True


class _FakeContainer:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def kill(self):
        self._log.append(f"kill:{self.name}")

    def remove(self, force=False):
        self._log.append(f"remove:{self.name}")


def _stub_docker(monkeypatch):
    """The `docker` SDK, stubbed into sys.modules — see this module's own
    docstring for why the real one must not be needed here."""
    if "docker" in sys.modules:
        return
    fake_docker = ModuleType("docker")
    fake_errors = ModuleType("docker.errors")

    class _APIError(Exception):
        pass

    fake_errors.APIError = _APIError
    fake_docker.errors = fake_errors
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)


@pytest.fixture(autouse=True)
def _clean_registry():
    execute_mod._RUN_CONTAINER_NAMES.clear()
    execute_mod._ABORTED_RUN_IDS.clear()
    yield
    execute_mod._RUN_CONTAINER_NAMES.clear()
    execute_mod._ABORTED_RUN_IDS.clear()


@pytest.fixture
def engine(monkeypatch):
    """A fake container engine. `engine.alive` is the set of container names
    that exist; `engine.log` records every kill/remove in order."""

    _stub_docker(monkeypatch)

    class _Engine:
        def __init__(self) -> None:
            self.alive: set[str] = set()
            self.log: list[str] = []
            self.looked_up: list[str] = []
            #: `is_aborted` sampled at the instant each kill was issued — this
            #: is how the "flag set BEFORE the kill" contract is asserted
            #: rather than assumed.
            self.abort_flag_at_kill: list[bool] = []
            self.redis: _FakeRedis | None = None
            self.run_id = ""

    engine = _Engine()

    class _Containers:
        def get(self, name):
            engine.looked_up.append(name)
            if name not in engine.alive:
                raise RuntimeError(f"no such container: {name}")
            container = _FakeContainer(name, engine.log)
            original_kill = container.kill

            def _kill():
                engine.abort_flag_at_kill.append(
                    execute_mod.is_aborted(engine.redis, engine.run_id))
                original_kill()

            container.kill = _kill
            return container

    class _Client:
        containers = _Containers()

    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/fake.sock")
    sys.modules["docker"].DockerClient = lambda **_kw: _Client()
    return engine


def _abort(engine, monkeypatch, run_id, *, store=None, **kwargs):
    r = _FakeRedis(store)
    engine.redis = r
    engine.run_id = run_id
    monkeypatch.setattr(execute_mod, "_redis_client", lambda _url: r)
    return execute_mod.abort_job(run_id, "redis://fake", **kwargs), r


# ---------------------------------------------------------------------------
# Resolution ladder
# ---------------------------------------------------------------------------

def test_the_live_registry_names_the_container_to_kill(engine, monkeypatch):
    """Mutation: drop the `remember_container` call after `containers.run` in
    `_run_job_blocking` and this falls through to the deterministic name."""
    execute_mod._RUN_CONTAINER_NAMES["run-1"] = "aw-runner-run-somethingelse"
    engine.alive.add("aw-runner-run-somethingelse")

    result, _r = _abort(engine, monkeypatch, "run-1")

    assert result["status"] == "killed"
    assert result["container"] == "aw-runner-run-somethingelse"
    assert result["resolved_by"] == "local registry"
    assert engine.log == ["kill:aw-runner-run-somethingelse",
                          "remove:aw-runner-run-somethingelse"]


def test_a_name_written_by_another_worker_is_read_from_redis(engine, monkeypatch):
    """The ten-uvicorn-worker case (aw-workspace Dockerfile:94): the worker
    taking this abort is NOT the one that spawned the container, so its local
    dict is empty and only the Redis mirror can answer.

    Mutation: remove the `r.set(...)` in `remember_container` (or the Redis
    step in `abort_candidates`) and this falls back to the deterministic name.
    """
    shared: dict = {}
    execute_mod.remember_container(_FakeRedis(shared), "run-2", "aw-warm-a-b")
    execute_mod._RUN_CONTAINER_NAMES.clear()  # a different worker's process
    engine.alive.add("aw-warm-a-b")

    result, _r = _abort(engine, monkeypatch, "run-2", store=shared)

    assert result["status"] == "killed"
    assert result["resolved_by"] == "Redis registry"


def test_the_deterministic_cold_name_is_the_fallback(engine, monkeypatch):
    """No registry entry anywhere (worker restarted, Redis lost) — a cold
    agent container is still named after its run id."""
    engine.alive.add("aw-runner-run-run-3")

    result, _r = _abort(engine, monkeypatch, "run-3")

    assert result["status"] == "killed"
    assert result["resolved_by"] == "deterministic cold name"


def test_a_monitor_run_is_reachable_by_its_truncated_name(engine, monkeypatch):
    """`_build_raw_kwargs` names monitor containers aw-runner-monitor-{id[:12]}."""
    run_id = "0123456789abcdef0123"
    engine.alive.add(f"aw-runner-monitor-{run_id[:12]}")

    result, _r = _abort(engine, monkeypatch, run_id)

    assert result["status"] == "killed"
    assert result["resolved_by"] == "deterministic monitor name"


def test_the_warm_name_is_only_tried_when_both_halves_are_supplied(engine, monkeypatch):
    """A warm container's name is keyed on (agent_id, session_id) and is NEVER
    derivable from a run id — so it is a candidate only when the caller sent
    both."""
    engine.alive.add("aw-warm-agent1-sess1")

    missing, _r = _abort(engine, monkeypatch, "run-4")
    assert missing["status"] == "not_found"

    found, _r2 = _abort(engine, monkeypatch, "run-4",
                        agent_id="agent1", session_id="sess1")
    assert found["status"] == "killed"
    assert found["resolved_by"] == "warm name"


# ---------------------------------------------------------------------------
# The race this card was filed for
# ---------------------------------------------------------------------------

def test_an_abort_for_a_finished_run_is_a_clean_not_found(engine, monkeypatch):
    """Nothing alive under any candidate name. This MUST be an ordinary 200
    answer, never an exception and never a 404: RunnerLLM retries 404 as a
    transient app-reload, so a 404 here means three aborts over three seconds
    for a run that was simply already done — the exact race that produced this
    card."""
    result, _r = _abort(engine, monkeypatch, "run-gone")

    assert result["status"] == "not_found"
    assert result["run_id"] == "run-gone"
    assert engine.log == []
    assert len(result["tried"]) == 2  # cold + monitor deterministic names


def test_abort_without_a_container_engine_still_answers(engine, monkeypatch):
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", None)
    result, _r = _abort(engine, monkeypatch, "run-5")
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# The abort flag — set BEFORE the kill, and honoured by the codex retry loop
# ---------------------------------------------------------------------------

def test_the_abort_flag_is_set_before_the_container_is_killed(engine, monkeypatch):
    """Mutation: move `mark_aborted` below the kill loop in `abort_job` and
    this fails. It matters because the codex retry loop reads the flag to
    decide whether to respawn — set after the kill, the window between the two
    is exactly where a respawn resurrects what was just killed."""
    execute_mod._RUN_CONTAINER_NAMES["run-6"] = "aw-runner-run-run-6"
    engine.alive.add("aw-runner-run-run-6")

    _abort(engine, monkeypatch, "run-6")

    assert engine.abort_flag_at_kill == [True], \
        "the abort flag must already be set when the kill fires"


def test_the_flag_is_set_even_when_nothing_was_found(engine, monkeypatch):
    """The container may not exist YET — a cold start spends most of its time
    pulling the image. The flag has to outlive this call so the spawn about to
    happen sees it."""
    r = _FakeRedis()
    engine.redis = r
    monkeypatch.setattr(execute_mod, "_redis_client", lambda _url: r)

    execute_mod.abort_job("run-7", "redis://fake")

    assert execute_mod.is_aborted(r, "run-7") is True


def test_the_codex_retry_loop_does_not_respawn_an_aborted_run(monkeypatch):
    """Trap 2 from the design: `_run_cold_agent_with_retry` respawns a fresh
    container between attempts, so an abort landing in that window would kill
    a container that is already gone and the loop would start a new one.

    Mutation: delete the `is_aborted` check in `_run_cold_agent_with_retry`
    and this test's `must not respawn` assertion fires.
    """
    from tests.test_execute_codex_rollout_retry import (  # noqa: PLC0415
        ROLLOUT_ERROR, _FakeContainer as _RetryContainer, _publish_capture)

    _publish_capture(monkeypatch)
    monkeypatch.setattr(execute_mod.time, "sleep", lambda _s: None)
    r = _FakeRedis()
    execute_mod.mark_aborted(r, "run-8")
    spawned = {"count": 0}

    class _Containers:
        def run(self, _image, **_kwargs):
            # Counted, NOT raised: the respawn site catches every Exception
            # and returns 1, so an assert here would be swallowed and this
            # test would pass with the guard removed.
            spawned["count"] += 1
            return _RetryContainer([ROLLOUT_ERROR], 1)

    class _Client:
        containers = _Containers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _Client(), "img", {}, _RetryContainer([ROLLOUT_ERROR], 1),
        "run-8", r, True)

    assert spawned["count"] == 0, "an aborted run must not be respawned"
    assert returncode == 1


def test_an_unaborted_codex_run_still_respawns(monkeypatch):
    """The guard above must not break the retry it guards — same setup, no
    abort flag, and the respawn happens as before."""
    from tests.test_execute_codex_rollout_retry import (  # noqa: PLC0415
        ROLLOUT_ERROR, _FakeContainer as _RetryContainer, _publish_capture)

    _publish_capture(monkeypatch)
    monkeypatch.setattr(execute_mod.time, "sleep", lambda _s: None)
    spawned = {"count": 0}

    class _Containers:
        def run(self, _image, **_kwargs):
            spawned["count"] += 1
            return _RetryContainer(['{"type":"turn.completed"}'], 0)

    class _Client:
        containers = _Containers()

    returncode = execute_mod._run_cold_agent_with_retry(
        _Client(), "img", {}, _RetryContainer([ROLLOUT_ERROR], 1),
        "run-9", _FakeRedis(), True)

    assert returncode == 0
    assert spawned["count"] == 1


# ---------------------------------------------------------------------------
# The done sentinel — warm only
# ---------------------------------------------------------------------------

def test_killing_a_warm_container_publishes_the_done_sentinel(engine, monkeypatch):
    """Trap 3: a warm container's `done` is published by the relay INSIDE it,
    which we just SIGKILLed — so nothing else ever will, and AP-MT's consumer
    blocks on the run's 900s timeout holding that session's lock (the
    12-minute stall at AP-MT cli.py:404-415).

    Mutation: remove the `_publish_done` call in `abort_job` and this fails.
    """
    execute_mod._RUN_CONTAINER_NAMES["run-10"] = "aw-warm-agent1-sess1"
    engine.alive.add("aw-warm-agent1-sess1")

    _result, r = _abort(engine, monkeypatch, "run-10")

    assert r.published == [{"done": "1", "returncode": "-9"}]


def test_killing_a_cold_container_publishes_no_done(engine, monkeypatch):
    """The other half of trap 3, and the reason it is a `startswith` and not
    an unconditional publish: the cold path's own `finally` in
    `_run_job_blocking` already publishes exactly one `done`, and a second is
    noise on the stream.

    Mutation: make the publish unconditional and this fails.
    """
    execute_mod._RUN_CONTAINER_NAMES["run-11"] = "aw-runner-run-run-11"
    engine.alive.add("aw-runner-run-run-11")

    _result, r = _abort(engine, monkeypatch, "run-11")

    assert r.published == []


# ---------------------------------------------------------------------------
# Registry bookkeeping
# ---------------------------------------------------------------------------

def test_a_killed_run_is_dropped_from_the_registry(engine, monkeypatch):
    shared: dict = {}
    execute_mod.remember_container(_FakeRedis(shared), "run-12", "aw-runner-run-run-12")
    engine.alive.add("aw-runner-run-run-12")

    _result, r = _abort(engine, monkeypatch, "run-12", store=shared)

    assert execute_mod.lookup_container(r, "run-12") is None


def test_forget_run_clears_both_the_name_and_the_flag():
    shared: dict = {}
    r = _FakeRedis(shared)
    execute_mod.remember_container(r, "run-13", "aw-runner-run-run-13")
    execute_mod.mark_aborted(r, "run-13")

    execute_mod._forget_run(r, "run-13")

    assert execute_mod.lookup_container(r, "run-13") is None
    assert execute_mod.is_aborted(r, "run-13") is False
    assert shared == {}


def test_the_registry_survives_an_unreachable_redis():
    """Every registry call takes `r=None` (or a Redis that raises) without
    blowing up — an abort that can only see this worker's own dict is still
    better than an abort that 500s."""
    execute_mod.remember_container(None, "run-14", "aw-runner-run-run-14")
    assert execute_mod.lookup_container(None, "run-14") == "aw-runner-run-run-14"

    execute_mod.mark_aborted(None, "run-14")
    assert execute_mod.is_aborted(None, "run-14") is True

    execute_mod._forget_run(None, "run-14")
    assert execute_mod.lookup_container(None, "run-14") is None


def test_registry_keys_are_namespaced_away_from_ap_mts_own():
    """AP-MT writes `run:{id}:container` on this SAME Redis db for containers
    on ITS host. If this app wrote there too, AP-MT's `kill_run` would resolve
    a name its own daemon has never heard of and `docker kill` it."""
    assert execute_mod._container_reg_key("x") == "runner:run:x:container"
    assert execute_mod._abort_flag_key("x") == "runner:run:x:aborted"


# ---------------------------------------------------------------------------
# Abort racing the spawn
# ---------------------------------------------------------------------------

def test_a_cold_run_is_killable_from_the_moment_it_spawns_and_forgotten_after(
        monkeypatch):
    """Mutations, both caught here: drop `remember_container` after
    `containers.run` and the registry is empty WHILE the agent runs (abort
    falls back to guessing); drop `_forget_run` from the `finally` and the
    entry outlives the container by its whole TTL, so a later abort for that
    id targets whatever inherited the name."""
    r = _FakeRedis()
    monkeypatch.setattr(execute_mod, "_redis_client", lambda _url: r)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/fake.sock")
    monkeypatch.setattr(execute_mod, "_build_container_kwargs",
                        lambda _job: ("img", ["argv"], {"name": "aw-runner-run-run-16"}, None))
    monkeypatch.setattr(execute_mod.execution_index, "start", lambda _rid: None)
    monkeypatch.setattr(execute_mod, "_publish_line", lambda *_a, **_k: None)

    during: list[str | None] = []

    class _Container:
        name = "aw-runner-run-run-16"

        def logs(self, **_kw):
            # Mid-run: exactly when an /abort would arrive.
            during.append(execute_mod.lookup_container(r, "run-16"))
            return iter([b'{"type":"result"}\n'])

        def wait(self):
            return {"StatusCode": 0}

    class _Images:
        def pull(self, _image):
            pass

        def get(self, _image):
            pass

    class _Containers:
        def run(self, _image, **_kwargs):
            return _Container()

    class _Client:
        images = _Images()
        containers = _Containers()

    _stub_docker(monkeypatch)
    sys.modules["docker"].DockerClient = lambda **_kw: _Client()

    execute_mod._run_job_blocking({"run_id": "run-16", "cli": "claude", "prompt": "hi"},
                                  "redis://fake")

    assert during == ["aw-runner-run-run-16"]
    assert execute_mod.lookup_container(r, "run-16") is None
    assert execute_mod.is_aborted(r, "run-16") is False


def test_a_warm_turn_registers_the_container_it_was_fed_to(monkeypatch):
    """A warm container's name is keyed on (agent_id, session_id), so
    `_dispatch_warm_turn` is the ONLY place an abort for this run can be
    taught what to kill.

    Mutation: drop the `remember_container` call there and every warm abort
    resolves nothing — the deterministic cold name it would fall back to never
    exists for a warm run.
    """
    from agents_platform_runners_app import warm_pool  # noqa: PLC0415

    _stub_docker(monkeypatch)
    monkeypatch.setattr(warm_pool, "get_generation", lambda _url: "epoch-1")
    monkeypatch.setattr(warm_pool, "get_or_create", lambda **_kw: "aw-warm-a1-s1")
    monkeypatch.setattr(warm_pool, "maybe_reap", lambda _client: None)
    monkeypatch.setattr(warm_pool, "dispatch_turn", lambda **_kw: None)

    class _Images:
        def pull(self, _image):
            pass

        def get(self, _image):
            pass

    class _Client:
        images = _Images()

    r = _FakeRedis()
    execute_mod._dispatch_warm_turn(
        _Client(), {"run_id": "run-17", "agent_id": "a1", "session_id": "s1",
                    "cli": "claude", "prompt": "hi"},
        "redis://fake", r)

    assert execute_mod.lookup_container(r, "run-17") == "aw-warm-a1-s1"


def test_a_run_aborted_during_its_image_pull_is_never_spawned(monkeypatch, tmp_path):
    """A cold start spends most of its wall-clock in `images.pull`, so an
    impatient /abort routinely lands BEFORE the container exists. It answers
    `not_found` (there genuinely is nothing to kill) and the flag it left
    behind is what stops the spawn that was about to follow.

    Mutation: remove the `is_aborted` check before `containers.run` and the
    agent starts anyway — the original bug, one second earlier.
    """
    r = _FakeRedis()
    monkeypatch.setattr(execute_mod, "_redis_client", lambda _url: r)
    monkeypatch.setattr(execute_mod, "CONTAINER_SOCKET", "/fake.sock")
    monkeypatch.setattr(execute_mod, "_build_container_kwargs",
                        lambda _job: ("img", ["argv"], {"name": "aw-runner-run-run-15"}, None))
    monkeypatch.setattr(execute_mod.execution_index, "start", lambda _rid: None)

    spawned: list = []

    class _Images:
        def pull(self, _image):
            # The abort lands here, exactly as it does in production.
            execute_mod.mark_aborted(r, "run-15")

        def get(self, _image):
            pass

    class _Containers:
        def run(self, _image, **_kwargs):
            spawned.append(_kwargs)
            raise AssertionError("must not spawn a container for an aborted run")

    class _Client:
        images = _Images()
        containers = _Containers()

    _stub_docker(monkeypatch)
    sys.modules["docker"].DockerClient = lambda **_kw: _Client()

    execute_mod._run_job_blocking({"run_id": "run-15", "cli": "claude", "prompt": "hi"},
                                  "redis://fake")

    assert spawned == []
    # The consumer on the other side must still be released, or AP-MT waits
    # out the full 900s timeout for a run that will never produce anything.
    assert {"done": "1", "returncode": "-9"} in r.published
