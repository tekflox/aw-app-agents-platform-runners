"""Real-Redis, real-two-processes coverage for the abort registry.

This app's routes are served by TEN uvicorn workers (aw-workspace's
Dockerfile:94), while a job's `container` object lives only in the ONE worker
whose thread spawned it. An abort POST therefore has roughly a 1-in-10 chance
of landing on that worker — so a registry that is only a process-local dict
passes every single-process test in this suite and then fails ~90% of the time
in production, silently. That is precisely the bug agents-platform-multitenant
fixed on its own side (its Etapa 4, `redis_streams.persist_container_name`),
reproduced here on the other end of the wire.

An in-memory fake cannot prove any of that, and neither can a same-process
test: `execute._RUN_CONTAINER_NAMES` would answer the lookup before Redis was
ever consulted. So the writer below is a genuinely SEPARATE python process,
with its own interpreter, its own module state and its own Redis connection —
the real topology, not a simulation of it.

Skipped unless RUN_REDIS_INTEGRATION_TESTS=1, the same convention
agents-platform-multitenant's test_redis_streams_container_name.py uses.
Note this app's release workflow (aw-marketplace's app-release.yml) installs
only `pytest jsonschema fastapi httpx uvicorn` and stands up no Redis, so
these tests SKIP in CI and have to be run by hand:

    RUN_REDIS_INTEGRATION_TESTS=1 AW_TEST_REDIS_URL=redis://127.0.0.1:6379/1 \
      python3 -m pytest tests/test_abort_registry_redis_integration.py -q

Mutation notes (2026-09-17, run against a real Redis):
- removing the `r.set(...)` from `remember_container` made
  `test_a_name_written_by_another_process_is_readable_here` fail on its
  `assert name == ...`.
- removing the `r.set(...)` from `mark_aborted` made
  `test_an_abort_flag_set_by_another_process_is_visible_here` fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_INTEGRATION_TESTS") != "1",
    reason="needs a real Redis server — set RUN_REDIS_INTEGRATION_TESTS=1 to run",
)

REDIS_URL = os.environ.get("AW_TEST_REDIS_URL", "redis://127.0.0.1:6379/1")


def _in_another_process(body: str) -> str:
    """Run `body` in a fresh interpreter that imports this app the same way a
    second uvicorn worker would — no shared module state with this process."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from agents_platform_runners_app import execute as execute_mod\n"
        f"r = execute_mod._redis_client({REDIS_URL!r})\n"
        + body
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"writer process failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def redis_client():
    from agents_platform_runners_app import execute as execute_mod

    r = execute_mod._redis_client(REDIS_URL)
    yield r
    r.close()


def test_a_name_written_by_another_process_is_readable_here(redis_client):
    """The whole point: worker A spawned the container, worker B takes the
    abort. B's local dict is empty and only the Redis mirror can answer."""
    from agents_platform_runners_app import execute as execute_mod

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    _in_another_process(
        f"execute_mod.remember_container(r, {run_id!r}, 'aw-warm-agent1-sess1')\n")

    # This process has never heard of that run.
    assert run_id not in execute_mod._RUN_CONTAINER_NAMES

    assert execute_mod.lookup_container(redis_client, run_id) == "aw-warm-agent1-sess1"
    execute_mod._forget_run(redis_client, run_id)


def test_the_resolution_ladder_finds_the_other_workers_container(redis_client):
    """`abort_candidates` is what `abort_job` actually walks — assert on it,
    not just on the raw key, so a ladder that stopped consulting Redis would
    fail here even with the mirror intact."""
    from agents_platform_runners_app import execute as execute_mod

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    _in_another_process(
        f"execute_mod.remember_container(r, {run_id!r}, 'aw-warm-agent2-sess2')\n")

    names = [name for name, _src in execute_mod.abort_candidates(redis_client, run_id)]
    assert names[0] == "aw-warm-agent2-sess2"
    execute_mod._forget_run(redis_client, run_id)


def test_an_abort_flag_set_by_another_process_is_visible_here(redis_client):
    """Trap 2's cross-worker half: the codex retry loop runs in the worker
    that owns the job, the abort arrives on whichever worker the tunnel
    picked. Without the mirror the loop respawns what was just killed."""
    from agents_platform_runners_app import execute as execute_mod

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    assert execute_mod.is_aborted(redis_client, run_id) is False

    _in_another_process(f"execute_mod.mark_aborted(r, {run_id!r})\n")

    assert run_id not in execute_mod._ABORTED_RUN_IDS
    assert execute_mod.is_aborted(redis_client, run_id) is True
    execute_mod._forget_run(redis_client, run_id)


def test_another_process_sees_a_forget_from_here(redis_client):
    from agents_platform_runners_app import execute as execute_mod

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    execute_mod.remember_container(redis_client, run_id, "aw-runner-run-x")
    execute_mod._forget_run(redis_client, run_id)

    seen = _in_another_process(
        f"print(execute_mod.lookup_container(r, {run_id!r}))\n")
    assert seen == "None"


def test_the_keys_do_not_collide_with_ap_mts_own(redis_client):
    """AP-MT writes `run:{id}:container` on this same db for containers on ITS
    host; if this app wrote there too, AP-MT's kill_run would resolve a name
    its own daemon has never heard of and `docker kill` it."""
    from agents_platform_runners_app import execute as execute_mod

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    execute_mod.remember_container(redis_client, run_id, "aw-runner-run-x")
    try:
        assert redis_client.get(f"run:{run_id}:container") is None
        assert redis_client.get(f"runner:run:{run_id}:container") == "aw-runner-run-x"
    finally:
        execute_mod._forget_run(redis_client, run_id)
