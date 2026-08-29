"""Push this workspace's resolved OTLP export target to
agents-platform-multitenant (Kanban ap-mt-tenant-log-routing).

AP-MT is one process shared by every tenant; it cannot pull
``GET /api/settings/observability`` from a tenant's own workspace itself (that
would mean AP-MT holding a live X-Api-Key for every tenant it serves, which it
never has — see agents-platform-multitenant's ``core/kanban_writer.py``
docstring). So this workspace pushes instead, over the same authenticated
channel ``POST /api/runners/register`` already uses
(``agents_platform_token``, minted the same way).

Two HTTP calls, not one:

1. **Local** — this workspace's own ``GET /api/settings/observability``, over
   loopback (this app is ``tier: inprocess``, so it runs *inside* the
   workspace server — see ``kanban_dispatch.py``'s module docstring for why
   loopback is right here and wrong from the gateway's stdio MCP child,
   which never calls this module).
2. **Remote** — AP-MT's ``POST /api/runners/observability``, same
   ``agents_platform_base``/``agents_platform_token`` config as
   ``routes.py``'s ``/register`` handler.

Fired by aw-workspace core right after ``PUT /api/settings/observability``
saves (see ``routes.py``'s ``/register-observability`` route, which core
calls over loopback) — a mode change reaches AP-MT within the same request
cycle, no polling delay. That route is also a standalone manual retry for
when the push side failed (e.g. AP-MT was briefly unreachable) while the
save itself still succeeded.
"""
from __future__ import annotations

import logging
import os

import httpx

from . import kanban_dispatch as kanban_dispatch_mod

log = logging.getLogger("aw_apps.agents_platform_runners.observability_push")

API_KEY_VAR = "AW_WORKSPACE_API_KEY"
CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")


def _from_env_file(name: str) -> str | None:
    # Duplicated from kanban_dispatch.py rather than imported — that copy is
    # private (leading underscore) to its own module; this is the same ~8
    # lines every module here that reads the workspace .env re-declares
    # (see e.g. plugin.py's _workspace_env).
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    try:
        with open(os.path.join(home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def _local_api_key() -> str | None:
    return os.environ.get(API_KEY_VAR) or _from_env_file(API_KEY_VAR)


class ObservabilityPushError(RuntimeError):
    """Either leg of the push (local read or AP-MT write) failed — logged and
    swallowed by the caller (the ``/register-observability`` route), never
    raised across a request."""


def _read_local_observability(*, timeout: float) -> dict:
    """This workspace's own resolved Observability config, over loopback.

    Raises :class:`ObservabilityPushError` on anything that keeps this from
    being a trustworthy read — a stale/wrong push is worse than a skipped
    one, since a stale endpoint could keep exporting to a target this
    workspace no longer means to use.
    """
    key = _local_api_key()
    if not key:
        raise ObservabilityPushError(
            f"{API_KEY_VAR} is not set — cannot read this workspace's own "
            "Observability settings")
    base = kanban_dispatch_mod.board_base_url(prefer_loopback=True)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base}/api/settings/observability",
                              headers={"X-Api-Key": key})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise ObservabilityPushError(f"could not read local observability settings: {exc}") from exc


def _push_to_platform(base: str, token: str, payload: dict, *, timeout: float) -> dict:
    """The remote leg — AP-MT's ``POST /api/runners/observability``. Raises
    :class:`ObservabilityPushError` on failure, same contract as
    ``_read_local_observability``, so ``push_once`` handles both legs
    identically."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base.rstrip('/')}/api/runners/observability", json=payload,
                               headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise ObservabilityPushError(
            f"agents-platform-multitenant rejected the push: {exc}") from exc


def push_once(config: dict, *, timeout: float = 20.0) -> dict:
    """Read this workspace's resolved Observability target and push it to
    AP-MT. Returns a small status dict for logging — never raises past this
    function; the ``/register-observability`` route just logs whatever
    comes back, whether it was called by core on save or triggered manually.
    """
    from .plugin import DEFAULT_AGENTS_PLATFORM_BASE  # local import: avoids a plugin<->this-module cycle

    token = config.get("agents_platform_token")
    if not token:
        return {"pushed": False, "reason": "agents_platform_token not configured"}

    try:
        settings = _read_local_observability(timeout=timeout)
    except ObservabilityPushError as exc:
        return {"pushed": False, "reason": str(exc)}

    resolved = settings.get("resolved") or None
    workspace = os.environ.get("AW_WORKSPACE", "aw")
    base = config.get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE
    payload = {
        "workspace": workspace,
        "endpoint": (resolved or {}).get("endpoint") or "",
        "api_key": (resolved or {}).get("api_key") or "",
    }

    try:
        result = _push_to_platform(base, token, payload, timeout=timeout)
    except ObservabilityPushError as exc:
        return {"pushed": False, "reason": str(exc)}
    return {"pushed": True, "mode": settings.get("mode"), **result}
