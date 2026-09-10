"""Relay this workspace's Notion token to agents-platform-multitenant
(Kanban ``architecture:notion-token-per-tenant-ap-mt-step1``).

Same push-not-pull reasoning as ``observability_push.py``: AP-MT cannot read
a tenant's own secret store, so the workspace hands over what AP-MT needs on
the channel it is already authenticated on (``agents_platform_token``). The
difference is which app owns which half, and that is the whole reason this
module is thin:

* **aw-app-notion owns the token.** It is the source of truth and the only
  component that reads the plaintext out of a secret store. It decides when
  to push, when to delete, and whether a reconcile found drift.
* **This app owns the way to AP-MT** — ``agents_platform_base`` and
  ``agents_platform_token`` are in THIS app's config, and an app cannot read
  another app's config (``src/apps/base.py``'s ``AppContext`` grants no such
  facade).

So aw-app-notion calls the routes in ``routes.py`` that wrap the three
functions below, and each app keeps the credential it owns. Nothing here
stores, logs or returns a token: the argument goes straight out, and the read
direction only ever carries a fingerprint.

``reconcile_once`` is the other half — it rides the SAME 360s tick as the
skills-sync reconcile (``plugin.py``'s ``_reconcile``) rather than adding a
cadence of its own, and does nothing but poke aw-app-notion's
``/apmt/sync``. The comparison deliberately lives over there, with the token.
"""
from __future__ import annotations

import logging
import os

import httpx

from . import kanban_dispatch as kanban_dispatch_mod
from . import observability_push as observability_push_mod

log = logging.getLogger("aw_apps.agents_platform_runners.notion_token_sync")

NOTION_APP_ID = "notion"
TIMEOUT_S = 20.0


class NotionTokenSyncError(RuntimeError):
    """AP-MT (or, for the reconcile, aw-app-notion) could not be reached or
    refused the call. Surfaced to the caller — aw-app-notion turns a delete
    failure into a failed logout, so this must never be swallowed here."""


class NotionTokenNotConfigured(NotionTokenSyncError):
    """This workspace has no agents-platform to relay to, so no copy of the
    token was ever pushed and none can be. Distinct because it is the one
    failure aw-app-notion's logout is allowed to ignore — see routes.py's
    ``_notion_token_failure``."""


def _platform(config: dict) -> tuple[str, str]:
    from .plugin import DEFAULT_AGENTS_PLATFORM_BASE  # local import: avoids a plugin<->this-module cycle

    token = (config or {}).get("agents_platform_token")
    if not token:
        raise NotionTokenNotConfigured("agents_platform_token is not configured")
    base = (config or {}).get("agents_platform_base") or DEFAULT_AGENTS_PLATFORM_BASE
    return base.rstrip("/"), token


def _workspace() -> str:
    return os.environ.get("AW_WORKSPACE", "aw")


def _request(config: dict, method: str, **kwargs) -> dict:
    base, token = _platform(config)
    url = f"{base}/api/runners/notion-token"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.request(method, url,
                                  headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.HTTPError as exc:
        raise NotionTokenSyncError(
            f"agents-platform-multitenant unreachable at {url}: {exc}") from exc
    if resp.status_code >= 400:
        # 503 here is the real one to read: AP-MT answers it when it has no
        # AGENTS_SECRET_KEY, i.e. it refused to store the token in the clear.
        raise NotionTokenSyncError(
            f"agents-platform-multitenant refused the call ({resp.status_code}): "
            f"{resp.text[:300]}")
    return resp.json()


def push(config: dict, token: str) -> dict:
    return _request(config, "POST", json={"workspace": _workspace(), "token": token})


def delete(config: dict) -> dict:
    return _request(config, "DELETE", params={"workspace": _workspace()})


def state(config: dict) -> dict:
    """``{"configured": bool, "token_fingerprint": str}`` — fingerprint only;
    AP-MT has no route that returns the token itself."""
    return _request(config, "GET", params={"workspace": _workspace()})


def reconcile_once(config: dict) -> dict:
    """Ask aw-app-notion to reconcile its token with AP-MT's copy.

    Never raises — this is called from a watchdog tick, where an exception is
    a stack trace every six minutes that nobody reads (same contract as
    ``observability_push.push_once``). A workspace without aw-app-notion
    installed reports that as a reason, not a failure.
    """
    if not (config or {}).get("agents_platform_token"):
        return {"reconciled": False, "reason": "agents_platform_token not configured"}
    key = observability_push_mod._local_api_key()
    if not key:
        return {"reconciled": False,
                "reason": f"{observability_push_mod.API_KEY_VAR} is not set"}
    base = kanban_dispatch_mod.board_base_url(prefer_loopback=True)
    url = f"{base}/api/apps/{NOTION_APP_ID}/apmt/sync"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, headers={"X-Api-Key": key})
    except httpx.HTTPError as exc:
        return {"reconciled": False, "reason": f"aw-app-notion unreachable at {url}: {exc}"}
    if resp.status_code == 404:
        return {"reconciled": False, "reason": "aw-app-notion is not installed"}
    if resp.status_code >= 400:
        return {"reconciled": False,
                "reason": f"aw-app-notion refused the reconcile ({resp.status_code}): "
                          f"{resp.text[:300]}"}
    try:
        return resp.json()
    except ValueError:
        return {"reconciled": False, "reason": "aw-app-notion returned a non-JSON body"}
