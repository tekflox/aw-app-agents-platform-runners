"""Provider for aw-workspace's ``contributes.agents`` surface.

aw-workspace declares the contract (see its ``src/apps/agents.py``) but has
no way to reach Agents Platform. This app does — it already holds the base
URL and the identity token — so it implements the provider method and turns
one app's declaration into REST calls against
agents-platform-multitenant::

    POST /api/models          -> Model
    POST /api/agent-configs   -> AgentConfig
    POST /api/agent-groups    -> AgentGroup
    POST /api/agents          -> Agent

**The order above is the contract, not an implementation detail.** An Agent
carries ``model_slug``, ``agent_config_slug`` and ``group_slug``, and the
platform stores them as plain slug references — a wrong order doesn't error,
it produces an agent pointing at three things that don't exist yet. Doing
the sequencing here is the whole reason the provider takes the entire
declaration in one call instead of one object at a time.

Create-if-absent, matched by slug, never updated
------------------------------------------------

Every create is preceded by a GET of the existing slugs for that kind, and
a **409 from the platform is treated as success-by-someone-else**, not an
error. Both are needed: the pre-check keeps the common re-activation path
quiet (this runs on every boot), and the 409 tolerance covers the races the
pre-check can't — two workspaces seeding the same tenant, or a user
creating the agent in the UI between our GET and our POST.

Nothing is ever updated and nothing is ever deleted. See the seed-once
rationale in aw-workspace's ``src/apps/agents.py``: an agent's system prompt
is exactly the field a user spends weeks tuning, and an app re-asserting its
own copy on every boot would erase that with no trace.

A failure here is logged and skipped, never raised — aw-workspace calls this
inside app activation, and an app whose features work but whose seeded agent
didn't land beats an app that refuses to install.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("aw_apps.agents_platform_runners.agent_provisioner")

DEFAULT_TIMEOUT = 20.0

#: kind -> (API path, fields the platform accepts). Anything an app declares
#: outside this set is dropped before the POST: agents-platform rejects an
#: unknown field with a 422, so forwarding the manifest verbatim would turn
#: every future manifest-only key into a hard seeding failure.
ENDPOINTS: dict[str, tuple[str, frozenset[str]]] = {
    "models": ("/api/models", frozenset({
        "slug", "provider", "model_id", "display_name", "params", "enabled",
    })),
    "agent_configs": ("/api/agent-configs", frozenset({
        "slug", "name", "description", "mcp_config", "extra_volumes",
        "permissions", "auto_compact_threshold_tokens",
    })),
    "groups": ("/api/agent-groups", frozenset({
        "slug", "name", "description", "instructions", "kanban_target_status",
        "capabilities",
    })),
    "agents": ("/api/agents", frozenset({
        "slug", "name", "description", "system_prompt", "inherit_from",
        "agent_config_slug", "group_slug", "kanban_target_status",
        "capabilities", "hidden_from_flow", "use_cases", "model_slug",
        "tool_specs", "skill_slugs", "params", "mcp_config", "extra_volumes",
        "permissions", "icon", "color",
    })),
}

#: Creation order — an Agent references the other three by slug.
ORDER = ("models", "agent_configs", "groups", "agents")

#: ``display_name`` is required by the platform's ModelIn but is pure
#: presentation; defaulting it from the slug spares every manifest a field
#: that carries no decision.
DEFAULTS: dict[str, dict[str, str]] = {
    "models": {"display_name": "slug"},
}


class AgentProvisioner:
    """Seeds one app's declared Agents Platform objects. Reusable per call."""

    def __init__(self, base: str, token: str, timeout: float = DEFAULT_TIMEOUT,
                 transport: httpx.BaseTransport | None = None):
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        # Test seam only — production always builds a real client. Keeps the
        # ordering + 409 behaviour testable without a live platform.
        self.transport = transport

    # ---- public ------------------------------------------------------------

    def seed(self, app_id: str, spec: dict[str, Any]) -> dict[str, int]:
        """Create every declared object that doesn't exist. Returns counts."""
        created: dict[str, int] = {}
        if not self.base or not self.token:
            log.warning(
                "agent seeding skipped for %s: agents_platform_base/"
                "agents_platform_token not configured", app_id,
            )
            return created
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(base_url=self.base, headers=headers,
                          timeout=self.timeout, transport=self.transport) as client:
            for kind in ORDER:
                entries = spec.get(kind) or []
                if not entries:
                    continue
                created[kind] = self._seed_kind(client, app_id, kind, entries)
        return {k: v for k, v in created.items() if v}

    # ---- internals ---------------------------------------------------------

    def _seed_kind(self, client: httpx.Client, app_id: str, kind: str,
                   entries: list[dict[str, Any]]) -> int:
        path, allowed = ENDPOINTS[kind]
        existing = self._existing_slugs(client, path, kind)
        count = 0
        for entry in entries:
            slug = str(entry.get("slug") or "").strip()
            if not slug:
                continue
            if slug in existing:
                log.debug("%s %r already exists, leaving it alone", kind, slug)
                continue
            body = _payload(kind, entry, allowed)
            try:
                resp = client.post(path, json=body)
            except httpx.HTTPError as exc:
                log.warning("failed to create %s %r from %s: %s", kind, slug, app_id, exc)
                continue
            if resp.status_code == 409:
                # Created between our GET and this POST, or by another
                # workspace against the same tenant. Same outcome we wanted.
                log.debug("%s %r already existed (409), leaving it alone", kind, slug)
                continue
            if resp.status_code >= 400:
                log.warning("failed to create %s %r from %s: HTTP %s %s",
                            kind, slug, app_id, resp.status_code, resp.text[:300])
                continue
            count += 1
            log.info("seeded %s %r from %s", kind, slug, app_id)
        return count

    def _existing_slugs(self, client: httpx.Client, path: str, kind: str) -> set[str]:
        """Slugs already present. An unreadable list yields the empty set —
        every create then falls through to its own 409 handling, which is the
        safe direction: we may log a redundant POST, never a silent update."""
        try:
            resp = client.get(path)
            resp.raise_for_status()
            return {str(row.get("slug")) for row in resp.json() if row.get("slug")}
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            log.warning("could not list existing %s (%s) — relying on 409s", kind, exc)
            return set()


def _payload(kind: str, entry: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """The declared entry reduced to fields the platform's schema accepts."""
    body = {k: v for k, v in entry.items() if k in allowed}
    for field, source in DEFAULTS.get(kind, {}).items():
        if not body.get(field):
            body[field] = entry.get(source, "")
    return body
