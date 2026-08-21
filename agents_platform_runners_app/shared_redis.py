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
3. The first candidate host that actually accepts a TCP connection on
   ``AW_SHARED_REDIS_PORT`` (default 6379). Result is cached process-wide.

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


def _accepts_tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def discover_host(port: int) -> str | None:
    """First candidate host with something listening on ``port``."""
    for host in candidate_hosts():
        if _accepts_tcp(host, port):
            return host
    return None


def resolve(config: dict | None = None) -> str | None:
    """The shared Redis URL, or None when discovery finds nothing.

    None means no candidate host answered on the Redis port — a real
    misconfiguration, so callers should keep failing loudly rather than
    publishing run events into the void.
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
    host = discover_host(port)
    _probed = True
    if not host:
        _cached = None
        log.error(
            "shared_redis_url is unset, AW_SHARED_REDIS_URL is unset, and nothing "
            "answered on :%s at any of %s — set the secret on this app's Settings",
            port, ", ".join(candidate_hosts()))
        return None

    db = os.environ.get("AW_SHARED_REDIS_DB", DEFAULT_REDIS_DB)
    _cached = f"redis://{host}:{port}/{db}"
    log.info(
        "shared_redis_url unset in app config — discovered %s by probing :%s "
        "(set the secret on this app's Settings to override)", _cached, port)
    return _cached


def reset_cache() -> None:
    """Forget the probed result — used by tests and on a settings save."""
    global _cached, _probed
    _cached, _probed = None, False
