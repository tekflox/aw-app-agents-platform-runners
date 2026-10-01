"""warm_pool.py — persistent ("warm") container mode for THIS Runner.

Supports claude and codex (2026-08-14 — see aw-warm-wrapper-codex /
aw-warm-relay-codex.py for codex's own JSON-RPC app-server wrapper; claude
keeps using aw-warm-wrapper / aw-warm-relay.py's simpler stream-json
passthrough). This module's own logic (get_or_create/drain/reap/generation
invalidation) is entirely CLI-agnostic — only `dispatch_turn`'s FIFO
payload shape branches on `cli`.

Ported from agents-platform-multitenant's ``backend/app/core/warm_pool.py``
(read that file's docstring for the full design rationale: session-keyed
containers, epoch/generation invalidation, drain-not-kill semantics, 30-min
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
import re
import shlex
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("aw_apps.agents_platform_runners.warm_pool")

WARM_LABEL = "aw.warm"
AGENT_ID_LABEL = "aw.agent_id"
SESSION_ID_LABEL = "aw.session_id"
EPOCH_LABEL = "aw.epoch"
CLI_LABEL = "aw.cli"

# Warm container lifetime — enforced INSIDE the container by aw-warm-wrapper
# / aw-warm-wrapper-codex itself (their own TTL watcher subshell, which
# drains gracefully at this mark rather than killing — see those scripts);
# this constant exists here only for callers/tests to reference the same
# number, never polled or enforced from out here. Reduced from the original
# 6h (21600) to 30 min on 2026-09-30 per product request.
WARM_TTL_S = 1800

# Grace the HOST-side TTL backstop in reap() gives the in-container watcher
# before stepping in: it condemns at WARM_TTL_S + WARM_TTL_SLACK_S, never at
# WARM_TTL_S itself. The watcher above is still the normal mechanism and goes
# first; this slack is what keeps the two from racing over the same container
# at the same instant. The backstop firing at all therefore MEANS an
# in-container watcher failed to fire — which is why it logs at WARNING.
# (Card 3ec5bf3b-9510-8106-91f6-d9b31722daa4: 228 containers up to 3h old
# against a 30-min TTL, all still `running`, because nothing outside the
# container ever checked.)
WARM_TTL_SLACK_S = 300

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

# Hard ceiling on how many warm containers may be alive at once, before
# get_or_create() refuses to spawn another and the caller falls back to a
# cold spawn. Each warm container holds a live CLI process at ~145MB, and
# until this existed there was no bound of any kind: a caller that opens a
# NEW session per call (the stateless /v1/chat/completions door does exactly
# that) fills the whole TTL window with containers — 228 of them in 51
# minutes at concurrency 4, 37.2GB, which took a 62GB host to 0GB free and
# made every subsequent fork() fail. 40 x ~150MB is ~6GB nominal.
# Overridable per workspace through this app's persisted config
# (`max_warm_containers`) — see `configure()`.
MAX_WARM_CONTAINERS = 40


class WarmPoolFull(RuntimeError):
    """Raised by `get_or_create()` when spawning a NEW warm container would
    take the pool past `max_warm()`.

    Deliberately a distinct type, not a bare RuntimeError: execute.py's warm
    branch catches exactly this one and falls through to the cold/ephemeral
    path, while every other failure there stays a hard spawn_error. A caller
    that cannot tell the two apart would turn "the pool is busy" into a
    failed run, which is the opposite of the point.

    Never raised on the REUSE path — a session that already has its
    container keeps it however full the pool is.
    """

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

# Resolved alongside it from the `max_warm_containers` config key.
_config_max_warm: int = MAX_WARM_CONTAINERS

_FALSEY = {"0", "false", "no", "off", ""}


def _truthy(raw: Any) -> bool:
    """Config values arrive as real booleans from a JSON-schema boolean
    field, but env vars (and a hand-edited config) are strings — accept both
    rather than treating the string "false" as true."""
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in _FALSEY


def _resolve_max_warm(raw: Any) -> int:
    """Pool ceiling from a config value, falling back to the default.

    Accepts an int or a numeric string (a hand-edited config, or a JSON
    schema that typed the field as text). Absent/None/empty means "use the
    default" — and so does 0, deliberately: an unlimited pool is the exact
    condition this ceiling exists to prevent, so there is no in-band way to
    ask for one. Anything <= 0 or unparseable is refused with a warning
    rather than silently clamped, because a typo'd ceiling that reads as
    "no limit" would restore the incident.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return MAX_WARM_CONTAINERS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("warm_pool: max_warm_containers=%r is not a number — "
                    "using the default %d", raw, MAX_WARM_CONTAINERS)
        return MAX_WARM_CONTAINERS
    if value <= 0:
        log.warning("warm_pool: max_warm_containers=%r is not a usable ceiling "
                    "(0 means 'default', not 'unlimited') — using %d",
                    raw, MAX_WARM_CONTAINERS)
        return MAX_WARM_CONTAINERS
    return value


def max_warm() -> int:
    """Current pool ceiling — see MAX_WARM_CONTAINERS and `configure()`."""
    return _config_max_warm


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

    Also resolves ``max_warm_containers`` (the pool ceiling, read back
    through `max_warm()`) from the same config, for the same durability
    reason — see `_resolve_max_warm`.
    """
    global _config_enabled, _config_max_warm
    raw = (config or {}).get("warm_container")
    _config_enabled = True if raw is None else _truthy(raw)
    _config_max_warm = _resolve_max_warm((config or {}).get("max_warm_containers"))
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


# Podman/docker emit `Created` with nanosecond precision and a bare "Z";
# datetime.fromisoformat only grew tolerance for both in 3.11, so normalise
# rather than depend on the interpreter version.
_ISO_SUBSECOND_RE = re.compile(r"(\.\d{6})\d+")


def _created_age_s(c, now: float) -> float | None:
    """Seconds since a container was created, or None when that cannot be
    determined.

    ``Created`` arrives in two different shapes from the same SDK, exactly
    as `list_containers` documents for ``Labels``: the abbreviated attrs
    ``containers.list()`` returns carry an int unix epoch, while a full
    inspect (``containers.get()`` / post-``reload()``) carries an ISO8601
    string. Both are accepted here so a caller never has to know which kind
    of container object it is holding.

    None means "unparseable", and every caller must treat that as "leave it
    alone" rather than as "old": the TTL backstop below force-starts a drain
    on what it believes is an expired container, and doing that on a guess
    would be worse than the leak it exists to stop.
    """
    raw = (getattr(c, "attrs", None) or {}).get("Created")
    # bool is an int subclass — it is never a timestamp.
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return now - float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _ISO_SUBSECOND_RE.sub(r"\1", raw.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive timestamp from a container engine is UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt.timestamp()


def _container_age_s(client, name: str, now: float) -> float | None:
    """`_created_age_s` for a container looked up by name — None if it has
    gone away or its Created is unreadable."""
    try:
        return _created_age_s(client.containers.get(name), now)
    except Exception:
        return None


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


def _condemn_over_ttl(c, name: str, now: float, *, ttl_s: int, slack_s: int) -> bool:
    """TTL backstop for ONE running, non-draining warm container: if it is
    past ``ttl_s + slack_s``, start the ordinary graceful drain on it.
    Returns whether it was condemned.

    Condemning is exactly what `get_or_create` does to a stale container —
    rename to `-draining-<ts>`, touch the drain flag — and nothing more. No
    stop(), no kill(), no remove(): the wrapper's own 15s poll loop closes
    stdin only after the current turn finishes, so even a genuinely in-flight
    turn on an over-TTL container runs to completion. Force-removal stays
    where it already was, in reap()'s wedged-drainer stage, which collects
    this container `drain_grace_s` after the rename if it is *still* running
    — and one that is, is wedged by definition.

    Taken under the same `_session_lock(agent_id, session_id)` that
    `get_or_create` holds, so a rename can never land in the middle of one
    (the labels come off the container itself; a container missing them is
    still condemned, just unlocked — an unlabelled warm container predates
    the labels and has no dispatch that could be racing it).
    """
    age = _created_age_s(c, now)
    if age is None or age <= ttl_s + slack_s:
        return False

    attrs = getattr(c, "attrs", None) or {}
    labels = attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {}
    agent_id, session_id = labels.get(AGENT_ID_LABEL), labels.get(SESSION_ID_LABEL)
    lock = _session_lock(agent_id, session_id) if agent_id and session_id else None

    if lock is not None:
        lock.acquire()
    try:
        stale_name = f"{name}-draining-{int(time.time())}"
        c.rename(stale_name)
        try:
            c.exec_run(["touch", "/home/ubuntu/.aw-warm/drain"])
        except Exception:
            # Renamed but unflagged: the stable name is already free, and the
            # wedged-drainer stage still collects it. Worth a line, not a
            # rollback.
            log.warning("warm_pool.reap: drain flag for over-TTL %s could not be "
                        "touched — leaving it to the wedged-drainer sweep",
                        stale_name, exc_info=True)
    finally:
        if lock is not None:
            lock.release()

    # WARNING, not INFO: by contract every warm container drains itself at
    # WARM_TTL_S from the inside. Reaching this line means that watcher did
    # not fire, which is a fault in the container, not routine housekeeping.
    log.warning("warm_pool.reap: condemned %s — %.0fs old, past the %ds TTL "
                "(+%ds slack); its in-container TTL watcher did not fire",
                name, age, ttl_s, slack_s)
    return True


def reap(client, *, drain_grace_s: int = DRAIN_GRACE_S,
         ttl_s: int = WARM_TTL_S, slack_s: int = WARM_TTL_SLACK_S) -> int:
    """Remove warm containers that can never serve another turn, and return
    how many were REMOVED (condemnations are counted and logged separately —
    they remove nothing yet, by design).

    Three kinds of garbage now, all created by the normal happy path:
      * **stopped** warm containers — every drained or TTL-expired one, since
        they are spawned with ``remove=False``;
      * **wedged drainers** — a `-draining-<ts>` container still running an
        hour after it was asked to exit;
      * **over-TTL survivors** — a `running`, non-draining container older
        than ``ttl_s + slack_s``, i.e. one whose in-container TTL watcher
        failed. These are *condemned* (drained), not removed; the wedged
        -drainer stage above collects them a grace period later if the drain
        does not take. See `_condemn_over_ttl`.

    That third stage resolves, rather than works around, this docstring's
    long-standing concession that an idle running container "is
    indistinguishable from one about to receive the next message" — which is
    still true. The backstop never needs that distinction: it decides on
    AGE, which the host can observe directly, and acts gracefully enough
    that being wrong about idleness costs nothing. Without it a `running`
    container with a dead watcher was simply immortal (card
    3ec5bf3b-9510-8106-91f6-d9b31722daa4).

    A live, correctly-named warm container WITHIN its TTL is still never
    touched: that is the pool."""
    removed = 0
    condemned = 0
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
                if _condemn_over_ttl(c, name, now, ttl_s=ttl_s, slack_s=slack_s):
                    condemned += 1
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
    if removed or condemned:
        log.info("warm_pool.reap: removed %d dead warm container(s), "
                 "condemned %d over-TTL one(s)", removed, condemned)
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


def _check_ceiling(client, name: str) -> None:
    """Raise `WarmPoolFull` if the pool is at `max_warm()`. Spawn path only.

    Counts only containers that actually hold a CLI process: `running`, and
    not `-draining-` (a drainer is on its way out and its replacement is
    normally the very spawn being checked). A listing that FAILS is not
    treated as "full" — refusing to go warm because the socket hiccuped
    would convert a transient engine error into a pool-wide cold fallback.
    """
    ceiling = max_warm()
    try:
        containers = client.containers.list(filters={"label": f"{WARM_LABEL}=1"})
    except Exception:
        log.warning("warm_pool: could not count warm containers before spawning %s "
                    "— allowing the spawn rather than failing closed", name,
                    exc_info=True)
        return
    live = sum(1 for c in containers
               if getattr(c, "status", None) == "running"
               and "-draining-" not in (getattr(c, "name", "") or ""))
    if live >= ceiling:
        raise WarmPoolFull(
            f"warm pool full ({live}/{ceiling}) — not spawning {name}")


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
    whole (up to 30 min) life. "drain" leaves the old container to finish on its own;
    "force" removes it now, for a process too wedged to notice a drain flag.
    Neither is reachable while a turn is in flight — this runs BEFORE the
    turn is fed in — which is what keeps aw-warm-relay.py, and therefore the
    user's chat, out of the blast radius.

    Raises `WarmPoolFull` when SPAWNING would take the pool past
    `max_warm()`; callers are expected to fall back to a cold spawn (see
    execute.py's warm branch). Reuse never raises.

    That ceiling is APPROXIMATE under concurrency, on purpose. The only
    locks here are per-session, so N dispatches for N different sessions can
    all pass the count before any of them has run — overshoot bounded by the
    worker concurrency, and accepted. A global spawn lock would make it
    exact and is deliberately NOT taken: it would serialize every spawn in
    the process behind `_wait_ready`'s 10s, turning a pool-wide cold
    fallback into a pool-wide queue, which is worse than being a few
    containers over a ceiling whose own value is a round number.
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
                # ...unless it is already past its TTL. reap()'s backstop is
                # about to condemn this exact container; handing it to a turn
                # first is how that condemnation turns into a mid-flight
                # rename. Treating it as stale here instead sends it down the
                # drain+respawn path below, which is where an expired
                # container was always supposed to go.
                age = _container_age_s(client, name, time.time())
                if age is None or age <= WARM_TTL_S:
                    return name
                log.warning("warm_pool: %s is %.0fs old, past the %ds TTL — not "
                            "reusing it; draining and respawning",
                            name, age, WARM_TTL_S)
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

        # Ceiling check, deliberately HERE: after any stale container of this
        # session was renamed to `-draining-` (so a 1-for-1 replacement never
        # counts itself and always passes) and after every `return name`
        # above (so reuse is never refused), but before anything is spawned.
        _check_ceiling(client, name)

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


def _with_claude_turn_context(prompt: str, run_id: str, notion_task_id: str | None,
                              source_device: str | None) -> str:
    """Prepend the CURRENT turn's identity to the prompt text itself, for claude only.

    Codex's per-turn staleness (card 3d25bf3b-9510-814a-acd9-d06f9c28d10b) was fixed
    by merging ``turn_env`` into the fresh subprocess env `aw-warm-relay-codex.py`
    spawns for every turn — real, because codex re-execs `codex exec resume` per
    turn. claude has no equivalent: `aw-warm-wrapper` spawns ONE claude process for
    the container's whole (up to 30 min) life, so nothing can push an updated
    NOTION_TASK_ID/AW_RUN_ID/AW_SOURCE_DEVICE into its OS environment after turn 1 —
    a process's env is fixed at exec() time, and there is no live-patch mechanism
    for it. `turn_env` (written above, every turn) has no reader on the claude side
    for exactly this reason.

    agents-platform-multitenant's own (separate) CliLLM warm-container path hit
    this identical problem and tried BASH_ENV first — verified dead-end: the Bash
    tool spawns commands via `/bin/sh` (dash in this image), which does not source
    BASH_ENV at all. Its shipped fix, reused verbatim here: put the current turn's
    values directly in the prompt text the model reads, independent of whichever
    shell a later `echo $NOTION_TASK_ID` runs under.

    NOT applied to a RAW turn (``dispatch_turn(raw_prompt=True)``). A raw turn is
    a CLI slash command — agents-platform sends ``/compact`` that way — and the
    claude CLI only recognises one at position 0 of the prompt. This header put
    it at ~position 230, so the model answered ``/compact`` as a chat message and
    no compaction happened: nine such runs on session 4d86e8e6 between
    2026-09-10 and 2026-09-11 wrote no ``compact_boundary`` at all, each billed
    against a ~542k-token context. It is the 2026-07-05 framing bug
    (``ap-auto-compact-not-compacting`` root cause #1) reproduced here. A raw
    turn also has nothing to lose by skipping this: it runs no tools, so nothing
    in it ever reads $NOTION_TASK_ID/$AW_RUN_ID.
    """
    parts = []
    if notion_task_id:
        parts.append(f"NOTION_TASK_ID={notion_task_id}")
    if source_device:
        parts.append(f"AW_SOURCE_DEVICE={source_device}")
    if run_id:
        parts.append(f"AW_RUN_ID={run_id}")
    if not parts:
        return prompt
    return (
        "[SYSTEM]\nExecution context for this turn: " + " ".join(parts) + ". "
        "$NOTION_TASK_ID/$AW_SOURCE_DEVICE/$AW_RUN_ID may read empty or stale via "
        "the Bash tool in a warm container — use the values above instead if so.\n\n"
        + prompt
    )


def dispatch_turn(*, client, name: str, run_id: str, prompt: str, cli: str = "claude",
                  notion_task_id: str | None = None,
                  source_device: str | None = None,
                  raw_prompt: bool = False,
                  gateway_bearer_token: str | None = None) -> None:
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

    claude's turn payload also carries the current turn's identity inline
    (see `_with_claude_turn_context`) — its OS env goes stale after turn 1
    with nothing able to refresh it, unlike codex's (see that function's
    docstring for the full story).

    ``gateway_bearer_token``, when given, is exported into this SAME
    ``turn_env`` file as ``AW_GATEWAY_BEARER_TOKEN`` — must match
    ``execute.py``'s ``CODEX_GATEWAY_TOKEN_ENV_VAR`` exactly, duplicated
    here rather than imported for the same reason ``GENERATION_KEY`` above
    is duplicated rather than shared. Codex's config.toml points its
    ``bearer_token_env_var`` at this name once, at container creation
    (see execute.py's ``_render_codex_config_toml``); re-exporting the
    CURRENT value here on every turn is what keeps a long-lived warm
    container from serving a gateway credential that went stale sometime
    after it was created — the whole reason this parameter exists instead
    of leaving the token baked in statically like it used to be. Only ever
    passed for ``cli == "codex"``; harmless if a caller passes it for
    claude too, since claude's own turn payload never reads this file's
    generic exports for auth.

    Uses the docker-py exec API's raw socket mode for the FIFO write (the
    original's subprocess `docker exec -i ... | cat > fifo_in` translated to
    this app's SDK-based docker access).
    """
    c = client.containers.get(name)

    turn_env = f"export AW_RUN_ID={_sh(run_id)}\nexport NOTION_TASK_ID={_sh(notion_task_id)}\nexport AW_SOURCE_DEVICE={_sh(source_device)}\n"
    if gateway_bearer_token:
        # Must match execute.py's CODEX_GATEWAY_TOKEN_ENV_VAR — see this
        # function's own docstring on why it rides in here, per turn.
        turn_env += f"export AW_GATEWAY_BEARER_TOKEN={_sh(gateway_bearer_token)}\n"
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
        content = prompt if raw_prompt else _with_claude_turn_context(
            prompt, run_id, notion_task_id, source_device)
        payload = (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode("utf-8")
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
