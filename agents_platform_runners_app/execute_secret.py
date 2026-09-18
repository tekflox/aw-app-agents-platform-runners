"""Auto-generate this app's own ``execute_secret`` if none is configured yet
(Kanban ``execute_secret nunca é auto-gerado — runner falha com 500 numa
workspace nova``).

Same 'credentials are re-asserted, content is seeded once' rule
``identity_token.py`` already applies to ``agents_platform_token``: a
freshly created workspace has nobody who has ever typed anything into this
app's Settings, so ``execute_secret`` (the shared secret
``require_execute_secret`` in routes.py demands as ``X-Runner-Secret`` on
every ``POST /execute``/``/abort``) stays empty forever unless something
mints it. Before this module, that "something" was a human — the only path
routes.py:59-77 has ever offered is a 500 telling the caller to go type a
value in by hand.

Unlike ``identity_token``'s two hops, this is one hop: there is no remote
authority to mint the value from (it's a shared secret this workspace and
agents-platform-multitenant just need to agree on, not a token asserting an
identity), so ``secrets.token_urlsafe`` generates it locally. The persist
hop is identical in shape and for the identical reason: ``config["config
_slug"]/config`` is the ONLY path that round-trips through aw-backend's
``AppInstall.config`` and is what a fresh worker/process picks back up on
restart — mutating ``self._live_config`` in memory alone would look correct
until the next restart wiped it.

Never overwrites an existing value — the entire point is "seeded once, then
left to whatever a human or a previous run put there". Never raises past
:func:`ensure_configured`, called from ``activate()`` (non-fatal by design)
before ``runner_registration.register_with_platform`` sends its first
``execute_secret`` to agents-platform-multitenant.
"""
from __future__ import annotations

import logging
import secrets

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.execute_secret")

CONFIG_SLUG = "agents-platform-runners"
TIMEOUT_S = 20.0


def _persist(api_url: str, api_key: str, secret: str) -> bool:
    url = f"{api_url.rstrip('/')}/api/apps/{CONFIG_SLUG}/config"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, json={"config": {"execute_secret": secret}},
                               headers={"X-Api-Key": api_key})
    except httpx.HTTPError as exc:
        log.warning("execute_secret: could not persist the generated secret at %s: %s", url, exc)
        return False
    if resp.status_code >= 400:
        log.warning("execute_secret: this workspace refused the config save (%s): %s",
                    resp.status_code, resp.text[:300])
        return False
    return True


def ensure_configured(config: dict) -> str | None:
    """Generate + persist an ``execute_secret`` if ``config`` doesn't have
    one yet.

    Returns the new secret on success — already persisted; the caller only
    needs this to also update its own in-memory copy (e.g.
    ``self._live_config``) before it registers with the platform in the
    same pass. Returns None, and leaves ``config`` untouched, when a secret
    is already configured or persistence failed. Never raises.
    """
    from .plugin import _workspace_env  # local import: avoids a plugin<->this-module cycle

    if (config or {}).get("execute_secret"):
        return None

    api_url = _workspace_env("AW_WORKSPACE_API_URL")
    api_key = _workspace_env("AW_WORKSPACE_API_KEY")
    if not api_url or not api_key:
        log.warning("execute_secret: AW_WORKSPACE_API_URL/AW_WORKSPACE_API_KEY "
                    "not set — could not persist a generated secret")
        return None

    secret = secrets.token_urlsafe(32)
    if not _persist(api_url, api_key, secret):
        return None

    log.info("execute_secret: generated and persisted a new execute_secret")
    return secret
