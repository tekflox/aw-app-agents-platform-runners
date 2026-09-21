"""Flip ``kanban_sweep_enabled`` on by default, exactly once per install
(Kanban ``auto-criar sweep de Ready cards + automatizar/verificar webhook do
Notion``, Architect decision, run dd61a8b1f1d84aef942fab002aa9e69f).

The sweep this flag gates (``plugin.py``'s ``_register_kanban_sweep_watchdog``)
already exists and already runs every 60s — it was shipped OFF on purpose, as
a dark cut-over from the monolith's Notion webhook (see that watchdog's own
docstring). Nobody ever flipped it, so today a card moved to Ready dispatches
nothing until a human ticks it by hand in Settings. This module is that flip,
done once by the app itself instead of by a person.

**Why a schema-default change alone cannot do this**, same lesson
``platform_base.py`` already documents for a different field: the value is
PERSISTED into every install's config row the moment anything saves it, and
``identity_token._persist()`` re-saves the *whole* config every ~6h from its
own refresh watchdog. A workspace that has ever gone through one of those
paths already has ``kanban_sweep_enabled: false`` frozen into its config row,
so changing the schema's ``default`` only ever reaches a config that has
never been saved — in practice, none of them. The flip has to happen here,
against the real persisted value, exactly once.

Same idiom as ``execute_secret.py``: POST ``/api/apps/<slug>/config`` with
``X-Api-Key``, which the workspace's own ``_merge_config`` folds over whatever
is already there (a partial save never drops sibling keys).

**Deliberately no re-assert watchdog**, unlike ``execute_secret.py``'s
``ensure_configured`` (which IS retried on a watchdog because a missing
secret is a hard failure on every dispatch). If a human unticks
``kanban_sweep_enabled`` after this ran once, that untick IS the entire
rollback this flag exists to offer — an ensure-forever watchdog would silently
undo it. The guard field below (``kanban_sweep_default_applied``) is checked
once and never revisited; it does not matter what value ``kanban_sweep_enabled``
holds afterwards, only whether the guard itself is already set.

Known gap, accepted rather than papered over: if the very first ``activate()``
attempt fails to persist (e.g. ``AW_WORKSPACE_API_KEY``/``AW_WORKSPACE_API_URL``
not readable yet on a brand-new workspace — see ``platform_base.py``'s own
resolution-order caveats), there is no retry. The next thing that can flip it
is another full app restart/update, which could be arbitrarily far away. This
mirrors the Architect's explicit call not to add a watchdog here, and is
recorded as a known cost rather than silently patched.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.kanban_sweep_default")

CONFIG_SLUG = "agents-platform-runners"
TIMEOUT_S = 20.0
APPLIED_FLAG = "kanban_sweep_default_applied"


def _persist(api_url: str, api_key: str) -> bool:
    url = f"{api_url.rstrip('/')}/api/apps/{CONFIG_SLUG}/config"
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, json={"config": {
                "kanban_sweep_enabled": True,
                APPLIED_FLAG: True,
            }}, headers={"X-Api-Key": api_key})
    except httpx.HTTPError as exc:
        log.warning("kanban_sweep_default: could not persist the default flip at %s: %s",
                    url, exc)
        return False
    if resp.status_code >= 400:
        log.warning("kanban_sweep_default: this workspace refused the config save (%s): %s",
                    resp.status_code, resp.text[:300])
        return False
    return True


def ensure_default_applied(config: dict) -> dict | None:
    """Flip ``kanban_sweep_enabled`` on, exactly once per install.

    Returns the two changed fields (already persisted) on success, so the
    caller can also update its own in-memory copy in the same pass. Returns
    None, and leaves ``config`` untouched, when the flip already happened
    (the guard field is present, whatever it or ``kanban_sweep_enabled``
    currently hold — a human's later untick must never be reasserted away)
    or when persistence failed. Never raises.
    """
    from .plugin import _workspace_env  # local import: avoids a plugin<->this-module cycle

    if (config or {}).get(APPLIED_FLAG):
        return None

    api_url = _workspace_env("AW_WORKSPACE_API_URL")
    api_key = _workspace_env("AW_WORKSPACE_API_KEY")
    if not api_url or not api_key:
        log.warning("kanban_sweep_default: AW_WORKSPACE_API_URL/AW_WORKSPACE_API_KEY "
                    "not set — could not persist the default flip")
        return None

    if not _persist(api_url, api_key):
        return None

    log.info("kanban_sweep_default: kanban_sweep_enabled flipped on by default (first install)")
    return {"kanban_sweep_enabled": True, APPLIED_FLAG: True}
