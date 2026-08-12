"""warm_pool.py — persistent ("warm") claude container mode for THIS Runner.

Ported from agents-platform-multitenant's ``backend/app/core/warm_pool.py``
(read that file's docstring for the full design rationale: session-keyed
containers, epoch/generation invalidation, drain-not-kill semantics, 6h
in-container TTL self-destruct). The DESIGN is reused as-is — this file only
translates the docker-ACCESS mechanism to match this app's own substrate:

* Original: asyncio + a subprocess ``docker`` CLI binary on agents-platform's
  own host.
* Here: the synchronous ``docker`` Python SDK against ``AW_CONTAINER_SOCKET``
  (execute.py's existing client), because every job already runs in its own
  worker THREAD (``execute.py::start_job``), not on an asyncio loop — so
  locks here are ``threading.Lock``, not ``asyncio.Lock``, and there is no
  ``await`` anywhere in this module.

Warm mode is ON by default since 0.32.0 and is switched off through this
app's own persisted config (``warm_container: false``) — see `enabled()` /
`configure()` for the precedence rules and for why the previous
host-env-only gate (``RUNNER_WARM_CONTAINER=1``, default OFF) could not be
kept. Callers (execute.py) must never invoke anything here unless
`enabled()` is true AND the job is for the "claude" CLI with both agent_id
and session_id set (warm containers are keyed by both, same as the
original).

**Not yet validated against a live podman socket** (ported 2026-08-08) —
this app's own containers:manage capability is only reachable from its own
long-lived process, not from a normal per-turn CLI session, so this couldn't
be exercised end-to-end while writing it. Flip RUNNER_WARM_CONTAINER=1 on a
throwaway agent/session first and watch `docker ps`/logs before trusting it
for real traffic.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import threading
import time
from typing import Any, Callable

log = logging.getLogger("aw_apps.agents_platform_runners.warm_pool")

WARM_LABEL = "aw.warm"
AGENT_ID_LABEL = "aw.agent_id"
SESSION_ID_LABEL = "aw.session_id"
EPOCH_LABEL = "aw.epoch"

# Same 6h backstop as agents-platform's warm_pool.py — enforced INSIDE the
# container by aw-warm-wrapper itself (its own TTL watcher subshell); this
# constant exists here only for callers/tests to reference the same number,
# never polled or enforced from out here.
WARM_TTL_S = 21600

# Mirrors agents-platform's warm_pool.GENERATION_KEY exactly — deliberately
# the SAME Redis key, on the SAME shared Redis instance (this app's
# shared_redis_url secret talks to the same instance as agents-platform's
# AP_REDIS_URL, per execute.py's own docstring), so a config-changed bump
# from EITHER side invalidates every warm container everywhere. Reusing this
# key rather than a runner-scoped one is intentional, not an accident.
GENERATION_KEY = "warm:config_generation"


ENV_VAR = "RUNNER_WARM_CONTAINER"

# Resolved from this app's persisted config by `configure()` (called from
# plugin.activate + plugin.on_config_saved). Default True: warm is the
# DEFAULT mode since 0.32.0, opt-OUT rather than opt-in.
_config_enabled: bool = True

_FALSEY = {"0", "false", "no", "off", ""}


def _truthy(raw: Any) -> bool:
    """Config values arrive as real booleans from a JSON-schema boolean
    field, but env vars (and a hand-edited config) are strings — accept both
    rather than treating the string "false" as true."""
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in _FALSEY


def configure(config: dict | None) -> bool:
    """Resolve warm mode from this app's persisted config and remember it.

    The switch lived in the HOST's environment (``RUNNER_WARM_CONTAINER=1``)
    until 0.32.0, which was not survivable: aw-remote-host's
    ``bootstrap/workspace/install.sh`` only forwards that var into the
    workspace container when the host's OWN aw-remote-host process has it
    set, so every workspace recreate (i.e. every update/deploy) silently
    dropped a hand-set flag and the whole feature turned itself back off —
    observed repeatedly, last on 2026-08-12. On a nested BYOD host the
    aw-remote-host process is itself containerised, so there is no reachable
    place to set that env durably from inside the workspace at all.

    App config, by contrast, is persisted in the workspace DB and round-trips
    through aw-backend's AppInstall.config, so it survives recreates,
    updates and reinstalls (see the ``public`` field's note in aw-app.json
    for the reinstall half of that reasoning).
    """
    global _config_enabled
    raw = (config or {}).get("warm_container")
    _config_enabled = True if raw is None else _truthy(raw)
    return enabled()


def enabled() -> bool:
    """True when warm containers should be used.

    Precedence: an explicitly-set ``RUNNER_WARM_CONTAINER`` env var still
    wins (kept as a per-host escape hatch — e.g. forcing warm off on a host
    whose podman socket can't sustain long-lived containers, without
    touching shared app config), otherwise the persisted config decides,
    which defaults to ON.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is not None and raw.strip() != "":
        return _truthy(raw)
    return _config_enabled


def warm_container_name(agent_id: str, session_id: str) -> str:
    return f"aw-warm-{agent_id}-{session_id}"


def get_generation(redis_url: str) -> str:
    """Current config generation, labeled onto every warm container at spawn
    time — a dispatch compares its own fresh read against that label to
    decide reuse vs drain+respawn. Missing/unreachable Redis reads as "0" —
    safe-by-default: every existing warm container looks stale until the
    first real bump, this never crashes a dispatch."""
    try:
        import redis as _redis
        r = _redis.from_url(redis_url, decode_responses=True,
                             socket_connect_timeout=3, socket_timeout=3)
        return r.get(GENERATION_KEY) or "0"
    except Exception:
        log.warning("warm_pool.get_generation: Redis read failed — treating as stale "
                    "(every warm container will drain+respawn)", exc_info=True)
        return "0"


def bump_generation(redis_url: str) -> None:
    """Invalidate every warm container in one cheap write — call whenever
    something on this app's side could invalidate an already-running one
    (this app restarting is the obvious trigger; wire more as they come up).
    Best-effort, never raises."""
    try:
        import redis as _redis
        r = _redis.from_url(redis_url, decode_responses=True,
                             socket_connect_timeout=3, socket_timeout=3)
        r.set(GENERATION_KEY, str(time.time()))
    except Exception:
        log.warning("warm_pool.bump_generation: Redis write failed — warm containers "
                    "will NOT be invalidated by this event", exc_info=True)


# (agent_id, session_id) -> lock serializing every get_or_create() call for
# that session's warm container — mirrors agents-platform's
# warm_pool._SESSION_LOCKS exactly (built for the same race: two
# near-simultaneous turns both trying to spawn/rename the same stable
# container name). Per-session, not per-agent or global, so two DIFFERENT
# sessions of the same agent never contend on each other's lock.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_LOCK = threading.Lock()


def _session_lock(agent_id: str, session_id: str) -> threading.Lock:
    key = f"{agent_id}:{session_id}"
    with _SESSION_LOCKS_LOCK:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[key] = lock
        return lock


def _labels(client, name: str) -> dict[str, str] | None:
    """Return the container's labels, or None if it doesn't exist."""
    try:
        c = client.containers.get(name)
    except Exception:
        return None
    return (c.attrs.get("Config", {}) or {}).get("Labels") or {}


def _is_running(client, name: str) -> bool:
    try:
        c = client.containers.get(name)
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def drain(client, name: str) -> None:
    """Ask a warm container to exit on its own — after its current turn (if
    any) finishes (uncapped wait) or within ~15s if idle. A flag file, NOT a
    signal: `docker exec <name> touch /home/ubuntu/.aw-warm/drain`; the
    in-container wrapper (aw-warm-wrapper) polls for it and exits by itself.

    Deliberately does NOT call container.kill()/stop() — mirrors
    agents-platform's warm_pool.drain() docstring exactly: that belongs
    solely to the hard-abort path, and mixing the two was explicitly
    rejected there after a "gracefully cancelled" container once survived
    16+ minutes. Keep this function free of kill/stop against docker,
    forever."""
    try:
        c = client.containers.get(name)
        c.exec_run(["touch", "/home/ubuntu/.aw-warm/drain"])
    except Exception:
        log.warning("warm_pool.drain: touch drain flag failed for %s", name, exc_info=True)


def _wait_ready(client, name: str, timeout_s: float = 10.0) -> None:
    """Bounded, coarse wait for the wrapper's ready marker right after
    spawning a brand-new warm container — one-time cost on creation only."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            c = client.containers.get(name)
            rc, _out = c.exec_run(["test", "-f", "/home/ubuntu/.aw-warm/ready"])
            if rc == 0:
                return
        except Exception:
            pass
        time.sleep(0.3)
    log.warning("warm_pool: %s did not report ready within %.0fs — proceeding anyway",
               name, timeout_s)


# (name, epoch_hash) -> (image, docker-SDK run kwargs) for a FRESH warm
# container. Must NOT set "name"/"detach"/"remove" — get_or_create() does.
BuildKwargs = Callable[[str, str], tuple[str, dict[str, Any]]]


def get_or_create(*, client, agent_id: str, session_id: str, epoch_hash: str,
                   build_kwargs: BuildKwargs) -> str:
    """Return the name of a running warm container whose epoch label matches
    epoch_hash — reusing it if so, otherwise draining any stale one
    (mismatched epoch, or present-but-dead) and spawning a fresh one under
    the SAME stable name (aw-warm-<agent_id>-<session_id>).

    Serialized per (agent_id, session_id) so two concurrent dispatches to
    the same session never race each other on the same rename/run — but two
    DIFFERENT sessions (even of the same agent) proceed fully in parallel.
    """
    lock = _session_lock(agent_id, session_id)
    with lock:
        name = warm_container_name(agent_id, session_id)
        labels = _labels(client, name)
        if labels is not None:
            if labels.get(EPOCH_LABEL) == epoch_hash and _is_running(client, name):
                return name
            # Stale — free the stable name immediately so the fresh spawn
            # below can take it, then drain the old one in the background.
            # Draining is uncapped by design and must never block this call.
            stale_name = f"{name}-draining-{int(time.time())}"
            try:
                client.containers.get(name).rename(stale_name)
                threading.Thread(target=drain, args=(client, stale_name),
                                 name=f"warm-drain-{stale_name}", daemon=True).start()
            except Exception:
                log.warning("warm_pool: rename of stale %s failed — force-removing instead",
                           name, exc_info=True)
                try:
                    client.containers.get(name).remove(force=True)
                except Exception:
                    pass

        image, kwargs = build_kwargs(name, epoch_hash)
        kwargs = dict(kwargs)
        kwargs["name"] = name
        kwargs["detach"] = True
        kwargs["remove"] = False  # long-lived — never auto-removed like the ephemeral path
        client.containers.run(image, **kwargs)
        _wait_ready(client, name)
        return name


# Bounded wait for the FIFO write — writing one line into a FIFO is normally
# sub-millisecond, so this is deliberately tight: a genuinely wedged reader
# on the other end never finishes it at any timeout, so the exact bound
# matters less than having one at all (mirrors agents-platform's
# warm_pool.FIFO_WRITE_TIMEOUT_S).
FIFO_WRITE_TIMEOUT_S = 10.0


def _sh(s: str | None) -> str:
    return shlex.quote(s or "")


def dispatch_turn(*, client, name: str, run_id: str, prompt: str,
                  notion_task_id: str | None = None,
                  source_device: str | None = None) -> None:
    """Feed one turn's prompt into the warm container's FIFO.

    Writes current_run_id + turn_env FIRST (so the relay tags the very next
    lines it reads with the right Redis stream key), then writes the
    stream-json payload into the FIFO. The relay (aw-warm-relay.py) publishes
    directly to ``run:{run_id}:events`` — the SAME stream key/schema
    execute.py's ephemeral path publishes to — so agents-platform's existing
    Redis-stream consumer needs zero changes to read a warm turn's output,
    INCLUDING its "done" sentinel: this function does not wait for the turn
    to finish, and doesn't need to — the relay publishes that sentinel
    itself the moment it sees claude's own ``{"type":"result",...}`` event.

    Uses the docker-py exec API's raw socket mode for the FIFO write (the
    original's subprocess `docker exec -i ... | cat > fifo_in` translated to
    this app's SDK-based docker access).
    """
    c = client.containers.get(name)

    turn_env = f"export NOTION_TASK_ID={_sh(notion_task_id)}\nexport AW_SOURCE_DEVICE={_sh(source_device)}\n"
    setup_cmd = (
        f"printf '%s' {_sh(run_id)} > /home/ubuntu/.aw-warm/current_run_id && "
        f"printf '%s' {_sh(turn_env)} > /home/ubuntu/.aw-warm/turn_env"
    )
    rc, out = c.exec_run(["sh", "-c", setup_cmd])
    if rc != 0:
        raise RuntimeError(
            f"warm_pool.dispatch_turn: failed to set current_run_id/turn_env on {name}: "
            f"{(out or b'').decode(errors='replace')}")

    payload = (json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n").encode("utf-8")
    exec_id = client.api.exec_create(
        c.id, ["sh", "-c", "cat > /home/ubuntu/.aw-warm/fifo_in"], stdin=True,
    )["Id"]
    sock = client.api.exec_start(exec_id, socket=True)
    try:
        raw = sock._sock if hasattr(sock, "_sock") else sock
        raw.settimeout(FIFO_WRITE_TIMEOUT_S)
        raw.sendall(payload)
    except Exception as e:
        raise RuntimeError(
            f"warm_pool.dispatch_turn: writing turn into {name}'s fifo did not complete "
            f"within {FIFO_WRITE_TIMEOUT_S:.0f}s — container is likely wedged: {e}") from e
    finally:
        try:
            raw.shutdown(1)  # SHUT_WR — signals EOF to the `cat > fifo_in` reader
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
