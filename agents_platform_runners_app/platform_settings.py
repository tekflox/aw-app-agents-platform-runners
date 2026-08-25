"""Push workspace-owned settings into Agents Platform on config save.

Some things belong in one place but have to be *typed* in another. The
OpenAI API key is the case this module exists for: the models that use it
are contributed by this app (``contributes.agents.models`` in aw-app.json),
this app's settings panel is where a workspace owner already goes to
configure the platform link, and the platform reads the key from its own
``Setting`` row — reachable only over its REST API.

Without this, configuring the key meant opening the Agents Platform UI
separately and knowing that Settings → ``openai_api_key`` is the row that
matters, while the app panel that seeded the models had no say in whether
they could run. Two places to look, no relationship between them stated
anywhere.

**Only pushes a non-empty value.** A blank field means "not configured
here", not "clear whatever the platform has" — the platform's own UI is
also allowed to set this key, and a save of an unrelated field on this
panel must not wipe it. Clearing is done in the platform UI, deliberately.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.platform_settings")

DEFAULT_TIMEOUT = 15.0

#: config field -> platform setting key. One entry today; the shape is the
#: point — the next workspace-owned platform setting is a line here, not
#: another bespoke call site.
PUSHED_SETTINGS: dict[str, str] = {
    "openai_api_key": "openai_api_key",
}


def push_settings(base: str, token: str, config: dict,
                  timeout: float = DEFAULT_TIMEOUT,
                  transport: httpx.BaseTransport | None = None) -> dict[str, bool]:
    """PUT each configured value onto the platform. Returns key -> ok.

    Never raises: this runs inside ``on_config_saved``, and a settings save
    that fails because the platform is momentarily down should still save
    everything else. A failure is logged with the status code so it is
    diagnosable from the app log rather than silent.
    """
    base = (base or "").rstrip("/")
    if not base or not token:
        log.warning("settings push skipped: agents_platform_base/token not configured")
        return {}

    pending = {sk: str(config.get(ck) or "").strip()
               for ck, sk in PUSHED_SETTINGS.items()
               if str(config.get(ck) or "").strip()}
    if not pending:
        return {}

    results: dict[str, bool] = {}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=timeout,
                          transport=transport) as client:
            for setting_key, value in pending.items():
                try:
                    resp = client.put(f"/api/settings/{setting_key}",
                                      json={"value": value})
                    ok = resp.status_code < 300
                    results[setting_key] = ok
                    if ok:
                        log.info("pushed setting %s to agents-platform", setting_key)
                    else:
                        log.warning("pushing setting %s failed: HTTP %s %s",
                                    setting_key, resp.status_code, resp.text[:200])
                except Exception as exc:  # noqa: BLE001 — never break a config save
                    results[setting_key] = False
                    log.warning("pushing setting %s failed: %s", setting_key, exc)
    except Exception as exc:  # noqa: BLE001 — client construction, same rule
        log.warning("settings push skipped: %s", exc)
    return results
