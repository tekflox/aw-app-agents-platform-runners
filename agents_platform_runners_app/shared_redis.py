"""Resolve the shared Redis URL this app publishes run events to.

Why this module exists (2026-08-08): ``shared_redis_url`` used to be a
REQUIRED, hand-entered secret on this app's Settings, with no default. A
freshly created aw-workspace therefore had a Runner that looked installed and
healthy but answered every ``POST /execute`` with ``500 shared_redis_url is
not configured on this app's Settings`` until a human went and pasted the URL
in — a manual step that survived neither a redeploy nor the creation of
another workspace. Same class of gap as agents-platform-multitenant carrying a
stale ``AP_REDIS_URL`` in a hand-maintained ``.env``.

The address is discoverable, so discover it — but by PROBING, not by guessing
from the routing table. Measured 2026-08-08 from inside a workspace agent
container: the default-route gateway is podman's (``10.89.0.1``) and has
nothing on :6379, while the shared Redis answers on the DOCKER bridge gateways
(``172.18.0.1``/``172.17.0.1``) — routable from here but never the default
route. Deriving the address from ``/proc/net/route`` alone would therefore
have produced a confidently wrong URL. Compose DNS names (``aw-sandbox``,
``aw-redis``) are not candidates at all: they do not resolve from inside this
workspace's nested podman netns, which is the very thing that broke
agents-platform-multitenant's own spawned containers.

Resolution order (first hit wins):

1. ``shared_redis_url`` in this app's config — an explicit operator override
   always beats discovery (e.g. a Redis that needs a password, or one that is
   not on any bridge gateway).
2. ``AW_SHARED_REDIS_URL`` env — lets a deployment bake the value in without
   touching per-workspace app settings.
3. The first candidate host that answers an authenticated ``PING`` on
   ``AW_SHARED_REDIS_PORT`` (default 6379) — ``AW_SHARED_REDIS_PASSWORD``/
   ``AW_SHARED_REDIS_USER`` (default user ``default``) if set. A host found
   but refusing that credential (or refusing none) is never cached as a
   passwordless URL — this was a bare TCP connect check until 2026-10-12,
   which would "discover" an auth-protected host and hand back a URL that
   fails on every subsequent command. Result is cached process-wide.

The db index defaults to 1 because that is the db agents-platform-multitenant
attaches its ``run:{run_id}:events`` consumer groups on; publishing to any
other db means RunnerLLM waits forever on a stream nobody writes. Override
with ``AW_SHARED_REDIS_DB`` if that deployment moves.
"""
from __future__ import annotations

import logging
import os
import socket
import struct

log = logging.getLogger("aw.app.agents-platform-runners.shared_redis")

DEFAULT_REDIS_PORT = 6379
DEFAULT_REDIS_DB = "1"
PROBE_TIMEOUT_S = 0.5

# Well-known docker bridge gateways on the podman host. Ordered after the
# container's own default route (which is usually right in a plain-docker
# deployment) but they are what actually answers in the nested-podman
# aw-remote-host topology this app ships into.
FALLBACK_GATEWAYS = ("172.18.0.1", "172.17.0.1", "host.docker.internal")

_cached: str | None = None
_probed = False


def default_gateway_ip() -> str | None:
    """This container's default-route gateway, or None if it can't be read.

    ``/proc/net/route`` columns are Iface, Destination, Gateway, ... with the
    two address columns as little-endian hex. The default route is the row
    whose Destination is ``00000000``.
    """
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return None
    for row in rows[1:]:
        fields = row.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except (ValueError, struct.error):
                continue
    return None


def candidate_hosts() -> list[str]:
    """Hosts to probe, most-likely first, de-duplicated."""
    hosts: list[str] = []
    gateway = default_gateway_ip()
    if gateway:
        hosts.append(gateway)
    for host in FALLBACK_GATEWAYS:
        if host not in hosts:
            hosts.append(host)
    return hosts


def _probe(host: str, port: int, user: str | None, password: str | None) -> str:
    """Real RESP ``PING`` against ``host:port``, authenticated if a
    credential is given. Returns ``"ok"`` (answered — authenticated when a
    credential was supplied, unauthenticated otherwise), ``"auth"`` (a
    Redis IS there but refuses us — no credential given and one is
    required, or the one given is wrong), or ``"down"`` (nothing
    answered). A bare TCP connect used to be enough when every Redis on
    this stack ran with no auth at all; once one requires it, a successful
    connect is no longer the same thing as a usable connection."""
    import redis as _redis_sync  # lazy, same convention as warm_pool.py/execute.py in this app

    client = _redis_sync.Redis(
        host=host, port=port, username=user, password=password,
        socket_connect_timeout=PROBE_TIMEOUT_S, socket_timeout=PROBE_TIMEOUT_S,
    )
    try:
        client.ping()
        return "ok"
    except _redis_sync.exceptions.AuthenticationError:
        return "auth"
    except _redis_sync.exceptions.ResponseError as e:
        if "NOAUTH" in str(e) or "WRONGPASS" in str(e):
            return "auth"
        return "down"
    # TimeoutError is listed explicitly because redis-py does NOT derive it
    # from ConnectionError (verified on the installed version:
    # issubclass(TimeoutError, ConnectionError) is False). Without it, a
    # candidate whose packets are dropped rather than refused — a firewall,
    # a half-applied VPN route — raises straight out of this function, past
    # discover_host and resolve, and kills the plugin's activate. "Nothing
    # answered" is exactly what this function exists to report as "down";
    # a silent drop is the most ordinary way for nothing to answer.
    #
    # Live on 2026-10-13: an aw-remote-host recreate left a tailscale exit
    # node selected with its scoping routes NOT installed, so the probe to
    # the bridge gateway was swallowed by the tunnel ACL. This app failed
    # to attach, never mounted /execute, and every Agents Platform run
    # dispatched to it came back 404 — for what should have been one
    # candidate reporting "down" and the loop moving on.
    except (_redis_sync.exceptions.TimeoutError,
            _redis_sync.exceptions.ConnectionError, OSError):
        return "down"
    finally:
        client.close()


def discover_host(port: int, user: str | None, password: str | None) -> tuple[str | None, str]:
    """First candidate host that answers on ``port``, and how it answered.

    Returns ``(host, "ok")`` when found and usable, ``(host, "auth")`` when
    found but refusing the given (or absent) credential — this already IS
    the shared Redis, just locked, so the remaining candidates are not
    worth trying — or ``(None, "down")`` when nothing answered at all.
    """
    for host in candidate_hosts():
        state = _probe(host, port, user, password)
        if state in ("ok", "auth"):
            return host, state
    return None, "down"


def resolve(config: dict | None = None) -> str | None:
    """The shared Redis URL, or None when discovery finds nothing usable.

    None means no candidate host answered on the Redis port, or one did
    but refused our credential (or the lack of one) — both are real
    misconfigurations, so callers should keep failing loudly rather than
    publishing run events into the void with a URL that will fail on every
    command.
    """
    global _cached, _probed

    configured = (config or {}).get("shared_redis_url")
    if configured:
        return configured

    env_url = os.environ.get("AW_SHARED_REDIS_URL")
    if env_url:
        log.info("shared_redis_url unset in app config — using AW_SHARED_REDIS_URL")
        return env_url

    if _probed:
        return _cached

    port = int(os.environ.get("AW_SHARED_REDIS_PORT", DEFAULT_REDIS_PORT))
    user = os.environ.get("AW_SHARED_REDIS_USER", "default")
    password = os.environ.get("AW_SHARED_REDIS_PASSWORD") or None
    host, state = discover_host(port, user, password)
    _probed = True

    if state == "auth":
        _cached = None
        log.error(
            "found a Redis at %s:%s but it refused us (%s) — set "
            "AW_SHARED_REDIS_PASSWORD (and AW_SHARED_REDIS_USER if not "
            "'default') in this app's environment; refusing to cache a "
            "passwordless URL that would fail on every command",
            host, port, "wrong credential" if password else "credential missing")
        return None

    if not host:
        _cached = None
        log.error(
            "shared_redis_url is unset, AW_SHARED_REDIS_URL is unset, and nothing "
            "answered on :%s at any of %s — set the secret on this app's Settings",
            port, ", ".join(candidate_hosts()))
        return None

    db = os.environ.get("AW_SHARED_REDIS_DB", DEFAULT_REDIS_DB)
    auth = f"{user}:{password}@" if password else ""
    _cached = f"redis://{auth}{host}:{port}/{db}"
    log.info(
        "shared_redis_url unset in app config — discovered %s by probing :%s "
        "(set the secret on this app's Settings to override)",
        f"redis://{host}:{port}/{db}" if not password else f"redis://{user}:***@{host}:{port}/{db}",
        port)
    return _cached


def reset_cache() -> None:
    """Forget the probed result — used by tests and on a settings save."""
    global _cached, _probed
    _cached, _probed = None, False
