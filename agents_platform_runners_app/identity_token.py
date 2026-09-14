"""Obtain a fresh ``agents_platform_token`` (an aw-backend identity JWT) and
persist it as this app's own config field (Kanban
``feature:ap-runners-auto-mint-identity-token``).

Same 'credentials are re-asserted, content is seeded once' rule
``agent_provisioner._refresh_credentials`` already applies to the gateway MCP
token, extended to the second credential this app holds. That module's
reasoning transfers verbatim: a token is not something a user tunes, it is
derived by this machine, and freezing it at first install produces a config
that looks perfect in the UI and has no working credential behind it.

Two hops, deliberately not one:

1. **Mint** — ``POST {AW_BACKEND_URL}/api/workspaces/{AW_WORKSPACE}/identity-token``
   with ``Authorization: Bearer {AW_WORKSPACE_HOST_TOKEN}``. aw-backend's own
   default TTL is requested (no ``expiry_seconds`` sent) — see
   ``aw-backend/src/api/routes/workspace_identity_token.py`` for why a
   longer one defeats the point of a refresh loop.
2. **Persist** — ``POST {AW_WORKSPACE_API_URL}/api/apps/agents-platform-runners/config``
   with ``X-Api-Key: {AW_WORKSPACE_API_KEY}``. This is NOT the same as
   mutating ``self._live_config`` in memory: the token is baked into
   ``apps/agents-platform-runners/mcp.json``'s env block by
   ``plugin.write_mcp_json``, read by a stdio child spawned inside the
   aw-mcp-gateway container that shares none of this process's memory. Only
   the config-save path rewrites that file and fires
   ``contributes.mcp.reload_on_save`` — an in-memory-only refresh leaves
   every MCP tool on the dead token while this app's own state looks fine.

Both hops, and the decode below, never raise past :func:`refresh` — called
from ``activate()`` (non-fatal by design) and from a watchdog tick, where an
exception is a stack trace nobody reads.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.identity_token")

CONFIG_SLUG = "agents-platform-runners"
TIMEOUT_S = 20.0

# Refresh once the token has used up this fraction of its total lifetime —
# not a fixed days-left threshold: that leaves only a fixed retry window if
# aw-backend is unreachable, while half-life scales with however long a
# token turns out to be (~15 days of headroom on a 30-day token).
HALF_LIFE_FRACTION = 0.5


def _decode_claims(token: str) -> dict | None:
    """``{"iat": ..., "exp": ...}`` decoded from the JWT payload WITHOUT
    verifying its signature — this module is deciding whether to refresh,
    not authenticating anyone, and holds no public key to check against
    anyway. Returns None on anything that isn't a 3-part JWT with numeric
    iat/exp claims: garbage, an empty string, a non-JWT secret pasted by
    hand are all treated identically as "cannot tell, refresh now"."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return {"iat": float(claims["iat"]), "exp": float(claims["exp"])}
    except Exception:
        return None


def needs_refresh(token: str | None, *, now: float | None = None) -> bool:
    """Missing, unparseable, expired, or past half its lifetime."""
    if not token:
        return True
    claims = _decode_claims(token)
    if claims is None:
        return True
    now = time.time() if now is None else now
    iat, exp = claims["iat"], claims["exp"]
    if now >= exp or exp <= iat:
        return True
    return (now - iat) / (exp - iat) >= HALF_LIFE_FRACTION


def _mint(base_url: str, workspace: str, host_token: str) -> str | None:
    url = f"{base_url.rstrip('/')}/api/workspaces/{workspace}/identity-token"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, headers={"Authorization": f"Bearer {host_token}"})
    except httpx.HTTPError as exc:
        log.warning("identity_token: could not reach aw-backend at %s: %s", url, exc)
        return None
    if resp.status_code >= 400:
        log.warning("identity_token: aw-backend refused the mint (%s): %s",
                    resp.status_code, resp.text[:300])
        return None
    try:
        token = resp.json().get("token")
    except ValueError:
        log.warning("identity_token: aw-backend returned a non-JSON body")
        return None
    if not token:
        log.warning("identity_token: aw-backend's response carried no 'token' field")
        return None
    return token


def _persist(api_url: str, api_key: str, token: str) -> bool:
    url = f"{api_url.rstrip('/')}/api/apps/{CONFIG_SLUG}/config"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, json={"config": {"agents_platform_token": token}},
                               headers={"X-Api-Key": api_key})
    except httpx.HTTPError as exc:
        log.warning("identity_token: could not persist the refreshed token at %s: %s", url, exc)
        return False
    if resp.status_code >= 400:
        log.warning("identity_token: this workspace refused the config save (%s): %s",
                    resp.status_code, resp.text[:300])
        return False
    return True


def refresh(config: dict) -> str | None:
    """Mint + persist a fresh token if the one in ``config`` needs it.

    Returns the new token on success — already persisted; the caller only
    needs this to also update its own in-memory copy (e.g.
    ``self._live_config``) before it writes mcp.json in the same pass.
    Returns None, and leaves ``config`` untouched, when no refresh was
    needed or any step failed. Never raises.
    """
    from .plugin import _workspace_env  # local import: avoids a plugin<->this-module cycle

    current = (config or {}).get("agents_platform_token")
    if not needs_refresh(current):
        return None

    backend_url = _workspace_env("AW_BACKEND_URL")
    workspace = _workspace_env("AW_WORKSPACE")
    host_token = _workspace_env("AW_WORKSPACE_HOST_TOKEN")
    if not backend_url or not workspace or not host_token:
        log.info("identity_token: AW_BACKEND_URL/AW_WORKSPACE/AW_WORKSPACE_HOST_TOKEN "
                 "not fully set — skipping auto-mint")
        return None

    token = _mint(backend_url, workspace, host_token)
    if not token:
        return None

    api_url = _workspace_env("AW_WORKSPACE_API_URL")
    api_key = _workspace_env("AW_WORKSPACE_API_KEY")
    if not api_url or not api_key:
        log.warning("identity_token: minted a fresh token but AW_WORKSPACE_API_URL/"
                    "AW_WORKSPACE_API_KEY are not set — could not persist it")
        return None

    if not _persist(api_url, api_key, token):
        return None

    log.info("identity_token: minted and persisted a fresh agents_platform_token")
    return token
