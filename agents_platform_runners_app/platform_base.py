"""Resolve this app's ``agents_platform_base`` config field IN CODE, not via
a schema default (Kanban bug:agents-platform-base-default-unreachable-on-byod,
Architect decision 2026-09-14, Option C).

Why a schema-default change alone is INERT: the value is PERSISTED into
every install's config row — ``src/apps/routes.py``'s
``config_with_defaults()`` writes it at save time, and
``identity_token._persist()`` re-saves the *whole* config every ~6h from the
refresh watchdog. Every workspace with auto-mint on already has the old
bridge address frozen in that row (verified live on this host,
2026-09-14), so a new default only ever applies to a config that has never
been saved — in practice, none of them. Resolution has to happen here, on
every read, and has to treat the historical literal as equivalent to
"never configured".

Order:

1. an explicit, non-legacy value already in ``config``
2. ``AW_AGENTS_PLATFORM_BASE`` — an env-var escape hatch for a deployment
   that genuinely wants something other than the derived public host (the
   config field can no longer express "I want the bridge address" once that
   value is treated as legacy, so this is how it says so instead)
3. derived as ``agents-platform.<apex of AW_BACKEND_URL>`` — the same
   one-line transform ``src/api/workspace_url.py``'s ``base_domain()``
   already applies to derive ``workspace.<apex>``
4. a static public fallback, for the rare case neither is set

Deliberately NOT a runtime probe-then-fallback: aw-backend's own
``agents_base_guard.py`` incident showed a reachable-but-wrong address can
answer for weeks before anyone notices — a successful probe does not prove
correctness, so this decision was already paid for once and isn't being
re-litigated here.

Leaf module — must never import :mod:`plugin` at module scope. plugin.py
imports THIS module (the one-directional edge is fine); the reverse would
be a cycle. notion_token_sync.py and observability_push.py used to dodge a
plugin<->them cycle with a local ``from .plugin import
DEFAULT_AGENTS_PLATFORM_BASE`` — they now import this module directly
instead, since it has no dependency on plugin at all.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("aw_apps.agents_platform_runners.platform_base")

ENV_VAR_NAME = "AW_AGENTS_PLATFORM_BASE"
STATIC_PUBLIC_BASE = "https://agents-platform.aw.tekflox.com"

# Every literal this app (constant or inline fallback) has ever pointed
# agents_platform_base at before this module existed — all of them only
# reachable when agents-platform-multitenant happens to run on the same
# physical host (the bridge gateway) or the same container (loopback) as
# the workspace, neither true for a real BYOD host. A value equal to one of
# these — persisted or freshly read — is treated as "unset", never as an
# explicit choice a deployment made on purpose.
LEGACY_LITERALS = frozenset({
    "http://172.18.0.1:10014",
    "http://127.0.0.1:10014",
    "http://localhost:10014",
})


def workspace_env(name: str) -> str:
    """A workspace-published env var, from this process or from the .env the
    server mirrors it into (0600, written at boot). Moved here from
    plugin.py unchanged (same helper, same callers) — needed at
    mcp.json-write time because the *reader* (a stdio child of the
    gateway's container) has neither, and needed here too, to read
    ``AW_BACKEND_URL`` / ``AW_AGENTS_PLATFORM_BASE`` for the same reason."""
    value = os.environ.get(name)
    if value:
        return value
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"), ".aw-workspace")
    try:
        with open(os.path.join(home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _derive_from_backend_url() -> str:
    """``agents-platform.<apex>`` from ``AW_BACKEND_URL``'s apex
    (``api.aw.tekflox.com`` -> ``agents-platform.aw.tekflox.com``) — the
    same transform ``src/api/workspace_url.py``'s ``base_domain()`` already
    uses to derive the workspace subdomain from the same env var."""
    backend = workspace_env("AW_BACKEND_URL").strip()
    if not backend:
        return ""
    try:
        host = urlparse(backend).hostname or ""
    except Exception:  # a malformed URL must never break resolution
        log.warning("could not parse AW_BACKEND_URL=%r", backend, exc_info=True)
        return ""
    apex = host[len("api."):] if host.startswith("api.") else host
    return f"https://agents-platform.{apex}" if apex else ""


def resolve(config: dict) -> str:
    """The address this app should call agents-platform-multitenant on.

    Called on every read (not cached) — config is a small in-memory dict
    already, and this has to notice a config save without a restart, same
    as every other consumer of ``self._live_config``.
    """
    explicit = str((config or {}).get("agents_platform_base") or "").strip()
    if explicit and explicit.rstrip("/") not in LEGACY_LITERALS:
        return explicit

    env_override = workspace_env(ENV_VAR_NAME).strip()
    if env_override:
        return env_override

    derived = _derive_from_backend_url()
    if derived:
        return derived

    return STATIC_PUBLIC_BASE
