"""The AP-MT mapping the Notion webhook needs before its manual dashboard
step 4 (re-pointing the subscription) can ever work (Kanban ``auto-criar
sweep de Ready cards + automatizar/verificar webhook do Notion``, Architect
decision, run dd61a8b1f1d84aef942fab002aa9e69f).

**Registering the subscription itself is impossible to automate** — Notion
only exposes that as a manual step in a connection's own Webhooks tab
(developers.notion.com/reference/webhooks; confirmed live). What IS
automatable, and did not exist anywhere before this module, is the row
agents-platform-multitenant's webhook route resolves a tenant through
(``api/notion_webhook.py``): without it, even a human completing step 4
lands on a 404 (``api/notion_webhook.py:146`` refuses — "never guess a
tenant").

Two AP-MT calls, both authenticated with THIS app's own
``agents_platform_token`` (the identity ``routes.py``'s other AP-MT proxies
already use — see ``_ap_target`` there, duplicated here for the same reason
``_from_env_file`` is duplicated across this app's modules):

* :func:`state` — ``GET /api/runners/notion-subscription``, translated into
  none/pending/verified for ``GET /status``.
* :func:`register` — ``POST /api/runners/notion-subscription``, the write a
  human triggers from this app's Settings screen after finishing Notion's
  dashboard step 4.

``register`` needs ``notion_workspace_id``/``integration_id`` to build that
row, and this app cannot read either directly: aw-app-notion owns the Notion
token (same boundary ``notion_token_sync.py`` already respects), so those two
ids come from a new read-only route there, ``GET /api/apps/notion/bot`` —
reached over loopback with the workspace's own ``X-Api-Key``, the same door
``notion_token_sync.py``/``observability_push.py`` already use to cross this
exact app boundary.
"""
from __future__ import annotations

import logging

import httpx

from . import kanban_dispatch as kanban_dispatch_mod
from . import observability_push as observability_push_mod
from . import platform_base as platform_base_mod

log = logging.getLogger("aw_apps.agents_platform_runners.notion_subscription")

TIMEOUT_S = 20.0
NOTION_APP_ID = "notion"


class NotionSubscriptionError(RuntimeError):
    """agents-platform-multitenant, or aw-app-notion's bot-identity lookup,
    could not be reached or refused the call."""


def _ap_target(config: dict) -> tuple[str, dict[str, str]]:
    """(base_url, auth headers) for an AP-MT call on this app's own identity
    — same shape as ``routes.py``'s private ``_ap_target``, kept separate
    because that one is a closure over ``build_routes``'s own ``cfg``."""
    token = str((config or {}).get("agents_platform_token") or "").strip()
    if not token:
        raise NotionSubscriptionError(
            "this app holds no agents_platform_token — agents-platform cannot "
            "be called on its behalf")
    return platform_base_mod.resolve(config).rstrip("/"), {"Authorization": f"Bearer {token}"}


def state(config: dict) -> dict:
    """``{"state": "none"|"pending"|"verified"|"unknown", ...}`` — never
    raises: called from ``GET /status``, where an exception would take the
    whole status route down over a diagnostic."""
    try:
        base, headers = _ap_target(config)
    except NotionSubscriptionError as exc:
        return {"state": "unknown", "reason": str(exc)}
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.get(f"{base}/api/runners/notion-subscription", headers=headers)
    except httpx.HTTPError as exc:
        return {"state": "unknown",
                "reason": f"agents-platform-multitenant unreachable: {exc}"}
    if resp.status_code >= 400:
        return {"state": "unknown",
                "reason": f"agents-platform-multitenant refused the call "
                          f"({resp.status_code}): {resp.text[:300]}"}
    try:
        rows = resp.json()
    except ValueError:
        return {"state": "unknown",
                "reason": "agents-platform-multitenant returned a non-JSON body"}
    if not rows:
        return {"state": "none"}
    # A tenant maps at most one Notion workspace in practice — the most
    # recently created row is the one Notion's dashboard step 4 will actually
    # point at, so report that one rather than guessing among several.
    row = rows[-1]
    return {
        "state": "verified" if row.get("verified") else "pending",
        "subscription_id": row.get("subscription_id"),
        "notion_workspace_id": row.get("notion_workspace_id"),
    }


def _notion_bot(config: dict) -> dict:
    """This workspace's Notion bot identity — ``workspace_id`` +
    ``integration_id`` — via aw-app-notion's read-only ``GET /bot``
    (loopback, ``X-Api-Key``). Raises on any failure; callers of
    :func:`register` treat that as fatal to the whole register call."""
    key = observability_push_mod._local_api_key()
    if not key:
        raise NotionSubscriptionError(
            f"{observability_push_mod.API_KEY_VAR} is not set — cannot reach "
            "aw-app-notion's bot identity over loopback")
    base = kanban_dispatch_mod.board_base_url(prefer_loopback=True)
    url = f"{base}/api/apps/{NOTION_APP_ID}/bot"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.get(url, headers={"X-Api-Key": key})
    except httpx.HTTPError as exc:
        raise NotionSubscriptionError(f"aw-app-notion unreachable at {url}: {exc}") from exc
    if resp.status_code == 404:
        raise NotionSubscriptionError("aw-app-notion is not installed")
    if resp.status_code == 409:
        raise NotionSubscriptionError(
            f"aw-app-notion has no Notion token configured: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise NotionSubscriptionError(
            f"aw-app-notion refused the bot lookup ({resp.status_code}): {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise NotionSubscriptionError("aw-app-notion returned a non-JSON body") from None


def register(config: dict, subscription_id: str) -> dict:
    """Upsert the AP-MT mapping for ``subscription_id`` — the half of
    Notion's dashboard step 4 that IS automatable. Raises
    :class:`NotionSubscriptionError` on any failure; this is a write a human
    explicitly triggered from Settings, not a watchdog tick, so the caller
    (``routes.py``) surfaces the failure rather than swallowing it.
    """
    subscription_id = (subscription_id or "").strip()
    if not subscription_id:
        raise NotionSubscriptionError("subscription_id is required")

    bot = _notion_bot(config)
    workspace_id = str(bot.get("workspace_id") or "").strip()
    integration_id = str(bot.get("integration_id") or "").strip()
    if not workspace_id:
        raise NotionSubscriptionError(
            "aw-app-notion's bot identity carried no workspace_id — is a Notion "
            "token configured there?")

    base, headers = _ap_target(config)
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(f"{base}/api/runners/notion-subscription",
                               json={"subscription_id": subscription_id,
                                     "notion_workspace_id": workspace_id,
                                     "integration_id": integration_id},
                               headers=headers)
    except httpx.HTTPError as exc:
        raise NotionSubscriptionError(
            f"agents-platform-multitenant unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise NotionSubscriptionError(
            f"agents-platform-multitenant refused the call ({resp.status_code}): "
            f"{resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise NotionSubscriptionError(
            "agents-platform-multitenant returned a non-JSON body") from None
