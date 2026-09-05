"""warm_pool.py — persistent ("warm") container mode for THIS Runner.

Supports claude and codex (2026-08-14 — see aw-warm-wrapper-codex /
aw-warm-relay-codex.py for codex's own JSON-RPC app-server wrapper; claude
keeps using aw-warm-wrapper / aw-warm-relay.py's simpler stream-json
passthrough). This module's own logic (get_or_create/drain/reap/generation
invalidation) is entirely CLI-agnostic — only `dispatch_turn`'s FIFO
payload shape branches on `cli`.

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
`enabled()` is true AND the job is for a warm-capable CLI (claude or codex)
with both agent_id and session_id set (warm containers are keyed by both,
same as the original).

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
CLI_LABEL = "aw.cli"

# Same 6h backstop as agents-platform's warm_pool.py — enforced INSIDE the
# container by aw-warm-wrapper itself (its own TTL watcher subshell); this
# constant exists here only for callers/tests to reference the same number,
# never polled or enforced from out here.
WARM_TTL_S = 21600

# How long drain() waits for a drained container to stop before leaving it to
# the periodic sweep. Comfortably longer than a normal turn; a genuinely long
# one just gets collected by reap() instead.
DRAIN_COLLECT_S = 900

# A `-draining-<ts>` container still running this long after being asked to
# leave is not going to leave on its own (its wrapper is wedged, or its turn
# never ended). reap() force-removes it then — a garbage-collection backstop,
# deliberately far outside any real turn's lifetime.
DRAIN_GRACE_S = 3600

# Minimum spacing between the sweeps maybe_reap() actually runs.
REAP_INTERVAL_S = 600

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
    forever.

    Once the wrapper HAS exited on its own, the stopped container is pure
    garbage — `get_or_create` spawns with ``remove=False`` (it must: a warm
    container outlives the run that created it), so nothing ever cleaned
    these up and they accumulated one per drain. 49 warm containers, 33 of
    them ``-draining-``, all long dead, were sitting on the podman host on
    2026-08-14. Removing a container that has already stopped is not the
    kill/stop this docstring forbids — that prohibition is about ending a
    RUNNING container, which this still never does."""
    try:
        c = client.containers.get(name)
        c.exec_run(["touch", "/home/ubuntu/.aw-warm/drain"])
    except Exception:
        log.warning("warm_pool.drain: touch drain flag failed for %s", name, exc_info=True)
        return
    # Bounded wait for the wrapper's own exit, then collect the corpse. A turn
    # still in flight is uncapped by design, so a container that outlasts this
    # is simply left to reap()'s later sweep rather than hurried along.
    deadline = time.monotonic() + DRAIN_COLLECT_S
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            c.reload()
            if c.status == "running":
                continue
            c.remove(force=False)
            log.info("warm_pool.drain: %s exited and was removed", name)
        except Exception:
            log.debug("warm_pool.drain: post-exit removal of %s failed", name, exc_info=True)
        return


_last_reap = 0.0
_reap_lock = threading.Lock()


def reap(client, *, drain_grace_s: int = DRAIN_GRACE_S) -> int:
    """Remove warm containers that can never serve another turn, and return
    how many went.

    Two kinds of garbage, both created by the normal happy path:
      * **stopped** warm containers — every drained or TTL-expired one, since
        they are spawned with ``remove=False``;
      * **wedged drainers** — a `-draining-<ts>` container still running an
        hour after it was asked to exit.

    Never touches a live, correctly-named warm container: those are the whole
    point of the pool, and one sitting idle between turns is indistinguishable
    from one about to receive the next message."""
    removed = 0
    try:
        containers = client.containers.list(all=True, filters={"label": f"{WARM_LABEL}=1"})
    except Exception:
        log.warning("warm_pool.reap: could not list warm containers", exc_info=True)
        return 0
    now = time.time()
    for c in containers:
        name = getattr(c, "name", "") or ""
        try:
            if c.status != "running":
                c.remove(force=True)
                removed += 1
                continue
            if "-draining-" not in name:
                continue
            try:
                started = int(name.rsplit("-draining-", 1)[1])
            except (IndexError, ValueError):
                continue
            if now - started > drain_grace_s:
                c.remove(force=True)
                removed += 1
                log.warning("warm_pool.reap: force-removed %s — still running %.0fs after drain",
                            name, now - started)
        except Exception:
            log.debug("warm_pool.reap: removal of %s failed", name, exc_info=True)
    if removed:
        log.info("warm_pool.reap: removed %d dead warm container(s)", removed)
    return removed


def _infer_cli(c) -> str | None:
    """Guess which CLI a warm container runs when it predates ``CLI_LABEL``.

    Every warm container alive before this label existed has no ``aw.cli`` —
    the one thing that already discriminates claude from codex on such a
    container is the entrypoint each spawn path bakes in: claude's
    ``_build_warm_kwargs_claude`` sets ``aw-warm-wrapper``, codex's
    ``_build_warm_kwargs_codex`` sets ``aw-warm-wrapper-codex`` (execute.py).
    Requires a full inspect (``c.reload()``) — the abbreviated attrs
    ``containers.list()`` returns have no ``Config.Entrypoint``."""
    try:
        c.reload()
    except Exception:
        return None
    entrypoint = (c.attrs.get("Config") or {}).get("Entrypoint") or []
    joined = " ".join(entrypoint)
    if "aw-warm-wrapper-codex" in joined:
        return "codex"
    if "aw-warm-wrapper" in joined:
        return "claude"
    return None


def list_containers(client, *, include_draining: bool = False) -> list[dict]:
    """Inventory of every warm container this engine knows about — the read
    path ``reap()``'s own listing call was never surfaced for.

    Raises whatever the docker client raises on a failed list. Callers must
    NOT swallow that into an empty result: "no warm containers" and "could
    not check" are different answers a caller cannot tell apart from ``[]``
    alone.
    """
    containers = client.containers.list(all=True, filters={"label": f"{WARM_LABEL}=1"})
    out: list[dict] = []
    for c in containers:
        name = getattr(c, "name", "") or ""
        draining = "-draining-" in name
        if draining and not include_draining:
            continue
        attrs = c.attrs or {}
        # containers.list()'s abbreviated attrs carry Labels at the top
        # level; a full inspect (post-reload, from a prior call) nests them
        # under Config instead — accept either shape.
        labels = attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {}
        cli = labels.get(CLI_LABEL)
        cli_source = "label"
        if not cli:
            cli, cli_source = _infer_cli(c), "inferred"
        out.append({
            "container_id": (getattr(c, "id", "") or "")[:12],
            "name": name,
            "status": getattr(c, "status", None),
            "session_id": labels.get(SESSION_ID_LABEL),
            "agent_id": labels.get(AGENT_ID_LABEL),
            "cli": cli,
            "cli_source": cli_source,
            "epoch": labels.get(EPOCH_LABEL),
            "created": attrs.get("Created"),
            "draining": draining,
        })
    return out


def maybe_reap(client) -> None:
    """Throttled, fire-and-forget reap() — safe to call on every dispatch.

    The sweep is a handful of API calls against the podman socket, but a
    dispatch is on the turn's critical path, so it runs at most every
    REAP_INTERVAL_S and always on a background thread."""
    global _last_reap
    with _reap_lock:
        if time.monotonic() - _last_reap < REAP_INTERVAL_S:
            return
        _last_reap = time.monotonic()
    threading.Thread(target=lambda: reap(client), name="warm-reap", daemon=True).start()


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
                   build_kwargs: BuildKwargs, recycle: str | None = None) -> str:
    """Return the name of a running warm container whose epoch label matches
    epoch_hash — reusing it if so, otherwise draining any stale one
    (mismatched epoch, or present-but-dead) and spawning a fresh one under
    the SAME stable name (aw-warm-<agent_id>-<session_id>).

    Serialized per (agent_id, session_id) so two concurrent dispatches to
    the same session never race each other on the same rename/run — but two
    DIFFERENT sessions (even of the same agent) proceed fully in parallel.

    ``recycle`` ("drain" | "force", from recycle_session — see execute.py's
    _dispatch_warm_turn) refuses to reuse an otherwise-perfectly-matching
    container, so the next turn gets a brand-new CLI process. That is the
    only lever there is over a dead MCP client: the clients are built once,
    when the CLI starts, and nothing re-initialises them for the container's
    whole 6h life. "drain" leaves the old container to finish on its own;
    "force" removes it now, for a process too wedged to notice a drain flag.
    Neither is reachable while a turn is in flight — this runs BEFORE the
    turn is fed in — which is what keeps aw-warm-relay.py, and therefore the
    user's chat, out of the blast radius.
    """
    lock = _session_lock(agent_id, session_id)
    with lock:
        name = warm_container_name(agent_id, session_id)
        labels = _labels(client, name)
        if labels is not None:
            if recycle:
                log.info("warm_pool: recycle=%s requested for %s — not reusing it", recycle, name)
                if recycle == "force":
                    try:
                        client.containers.get(name).remove(force=True)
                    except Exception:
                        log.warning("warm_pool: force-remove of %s failed — falling through "
                                    "to the drain path", name, exc_info=True)
                    else:
                        labels = None
            elif labels.get(EPOCH_LABEL) == epoch_hash and _is_running(client, name):
                return name
        if labels is not None:
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


def dispatch_turn(*, client, name: str, run_id: str, prompt: str, cli: str = "claude",
                  notion_task_id: str | None = None,
                  source_device: str | None = None) -> None:
    """Feed one turn's prompt into the warm container's FIFO.

    Writes current_run_id + turn_env FIRST (so the relay tags the very next
    lines it reads with the right Redis stream key), then writes the turn
    payload into the FIFO — shaped per `cli`, since each CLI's in-container
    reader expects a different envelope (see the branch below). Either
    relay (aw-warm-relay.py for claude, aw-warm-relay-codex.py for codex)
    publishes directly to ``run:{run_id}:events`` — the SAME stream key/
    schema execute.py's ephemeral path publishes to — so agents-platform's
    existing Redis-stream consumer needs zero changes to read a warm turn's
    output, INCLUDING its "done" sentinel: this function does not wait for
    the turn to finish, and doesn't need to — the relay publishes that
    sentinel itself the moment it sees the CLI's own turn-complete event
    (claude: ``{"type":"result",...}``; codex: a ``turn/completed``
    notification for this container's thread).

    Uses the docker-py exec API's raw socket mode for the FIFO write (the
    original's subprocess `docker exec -i ... | cat > fifo_in` translated to
    this app's SDK-based docker access).
    """
    c = client.containers.get(name)

    turn_env = f"export AW_RUN_ID={_sh(run_id)}\nexport NOTION_TASK_ID={_sh(notion_task_id)}\nexport AW_SOURCE_DEVICE={_sh(source_device)}\n"
    setup_cmd = (
        f"printf '%s' {_sh(run_id)} > /home/ubuntu/.aw-warm/current_run_id && "
        f"printf '%s' {_sh(turn_env)} > /home/ubuntu/.aw-warm/turn_env"
    )
    rc, out = c.exec_run(["sh", "-c", setup_cmd])
    if rc != 0:
        raise RuntimeError(
            f"warm_pool.dispatch_turn: failed to set current_run_id/turn_env on {name}: "
            f"{(out or b'').decode(errors='replace')}")

    if cli == "codex":
        # Plain JSON object, not claude's stream-json envelope — read one
        # line at a time by aw-warm-relay-codex.py, which owns codex's
        # app-server connection itself and turns this into a turn/start
        # JSON-RPC call (see that script's module docstring for why the
        # request/response correlation has to live there instead of here).
        payload = (json.dumps({"prompt": prompt}) + "\n").encode("utf-8")
    else:
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
