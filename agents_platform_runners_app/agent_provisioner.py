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
    POST /api/agent-flows     -> AgentFlow

**The order above is the contract, not an implementation detail.** An Agent
carries ``model_slug``, ``agent_config_slug`` and ``group_slug``, and an
AgentFlow's ``graph`` names agents by slug — the platform stores all of
these as plain slug references, so a wrong order doesn't error, it produces
an object pointing at things that don't exist yet. Doing the sequencing
here is the whole reason the provider takes the entire declaration in one
call instead of one object at a time.

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

import json
import logging
import os
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
    "agent_flows": ("/api/agent-flows", frozenset({
        "slug", "name", "description", "enabled", "graph", "max_hops",
        "budget_tokens", "budget_usd",
    })),
}

#: Creation order — an Agent references the other three by slug, and an
#: AgentFlow's graph references agents by slug, so it goes last.
ORDER = ("models", "agent_configs", "groups", "agents", "agent_flows")

#: ``display_name`` is required by the platform's ModelIn but is pure
#: presentation; defaulting it from the slug spares every manifest a field
#: that carries no decision.
DEFAULTS: dict[str, dict[str, str]] = {
    "models": {"display_name": "slug"},
}


class AgentProvisioner:
    """Seeds one app's declared Agents Platform objects. Reusable per call."""

    def __init__(self, base: str, token: str, timeout: float = DEFAULT_TIMEOUT,
                 transport: httpx.BaseTransport | None = None,
                 mcp_url_overrides: dict[str, str] | None = None):
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        # Test seam only — production always builds a real client. Keeps the
        # ordering + 409 behaviour testable without a live platform.
        self.transport = transport
        # server name -> URL to use instead of the one in .mcp.json. See
        # resolve_mcp_servers: the token is shared, only the address differs
        # between this container's network view and a spawned agent's.
        self.mcp_url_overrides = mcp_url_overrides or {}

    # ---- public ------------------------------------------------------------

    def seed(self, app_id: str, spec: dict[str, Any]) -> dict[str, int]:
        """Create every declared object that doesn't exist. Returns counts."""
        created: dict[str, int] = {}
        # Expand `mcp_servers: ["aw-gateway"]` into the real connection dict
        # before anything is POSTed — see apply_mcp_references below for why
        # the credential is resolved here and not carried in the manifest.
        spec = apply_mcp_references(spec, url_overrides=self.mcp_url_overrides)
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
                # Content is seeded once and never rewritten — but a
                # CREDENTIAL is not content. See _refresh_credentials.
                self._refresh_credentials(client, app_id, kind, path, slug, entry)
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

    def _refresh_credentials(self, client: httpx.Client, app_id: str, kind: str,
                             path: str, slug: str, entry: dict[str, Any]) -> None:
        """Re-assert the resolved ``mcp_config`` on an object that already exists.

        Seed-once protects what a USER tunes — a system prompt, a model
        choice, a flow graph. A gateway bearer token is not that. It is
        derived by this machine at resolve time, nobody types it, and it
        stops working the moment the gateway rotates it. Freezing it at
        first install produces an agent whose config looks perfect in the UI
        and has no MCP surface at all: the gateway 401s, the client
        registers zero tools, and nothing anywhere reports it.

        So the rule this provider implements is narrower than "never
        update": **content is seeded once, credentials are re-asserted on
        every activation.** Only entries that declared ``mcp_servers``
        (by-name, credential-free — see apply_mcp_references) are touched,
        and only their ``mcp_config`` field, so a user who edited a prompt
        or swapped a model keeps that edit.

        Failure is logged and swallowed: a stale token is bad, an app that
        won't activate is worse.
        """
        # Only by-reference declarations. An app that spelled mcp_config out
        # by hand owns it, and a user's hand-edit must survive.
        if not entry.get("_mcp_by_reference") or not entry.get("mcp_config"):
            return
        try:
            resp = client.put(f"{path}/{slug}", json={"mcp_config": entry["mcp_config"]})
        except httpx.HTTPError as exc:
            log.warning("could not refresh mcp credentials on %s %r from %s: %s",
                        kind, slug, app_id, exc)
            return
        if resp.status_code >= 400:
            log.warning("could not refresh mcp credentials on %s %r from %s: HTTP %s",
                        kind, slug, app_id, resp.status_code)
            return
        log.info("refreshed mcp credentials on %s %r from %s", kind, slug, app_id)

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


# --- mcp_servers by reference ------------------------------------------------
#
# An agent that can't reach the workspace's MCP gateway has no Kanban, no
# knowledge base and no platform tools — for most contributed agents that is
# the difference between working and not. But the gateway entry is
# ``{url, headers: {Authorization: Bearer <token>}}``, and a manifest is a
# public artefact that ships to a marketplace, so an app cannot simply
# declare it.
#
# So an app declares the server it wants BY NAME::
#
#     "agent_configs": [
#       {"slug": "...", "name": "...", "mcp_servers": ["aw-gateway"]}
#     ]
#
# and this module resolves each name into the real connection dict at seed
# time, here, inside the workspace that owns the secret. The manifest carries
# an intention; the credential never leaves the machine.
#
# Resolution reads the workspace's own canonical ``.mcp.json`` (AGENTS.md:
# "``.mcp.json`` at the repo root is the canonical config"), which the
# gateway app already writes itself on boot — so a freshly created workspace
# resolves correctly with nothing pasted by hand, which was the whole point.

#: Where the workspace's canonical MCP config lives.
MCP_CONFIG_PATH = os.path.join(
    os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"), ".mcp.json"
)


def resolve_mcp_servers(
    names: list[str], *, url_overrides: dict[str, str] | None = None,
    config_path: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Turn ``["aw-gateway"]`` into the servers dict an AgentConfig stores.

    Unknown names, an unreadable ``.mcp.json`` and a missing entry all
    resolve to "not included" rather than raising: a contributed agent that
    comes up without its gateway is degraded, while an app that refuses to
    install is broken. Each omission is logged — this is precisely the kind
    of gap that otherwise shows up as an agent mysteriously having no tools.

    ``url_overrides`` exists because the URL in ``.mcp.json`` is written for
    THIS container's network view (``http://aw-app-mcp-gateway:9200/mcp``),
    and a spawned agent container sits in a different one, where that name
    does not resolve. The token is identical in both; only the address
    differs, so the address is the one thing configurable.
    """
    resolved: dict[str, dict[str, Any]] = {}
    if not names:
        return resolved
    path = config_path or MCP_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            servers = (json.load(fh) or {}).get("mcpServers") or {}
    except (OSError, ValueError) as exc:
        log.warning(
            "could not read %s (%s) — contributed agents will be seeded "
            "WITHOUT their declared MCP servers %s", path, exc, names,
        )
        return resolved

    overrides = url_overrides or {}
    for name in names:
        entry = servers.get(name)
        if not isinstance(entry, dict) or not entry.get("url"):
            log.warning(
                "MCP server %r is not present in %s — the agent config "
                "declaring it will be seeded without it", name, path,
            )
            continue
        server = {
            "type": entry.get("type") or "streamable-http",
            "url": overrides.get(name) or entry["url"],
        }
        if entry.get("headers"):
            server["headers"] = dict(entry["headers"])
        resolved[name] = server
    return resolved


def apply_mcp_references(
    spec: dict[str, Any], *, url_overrides: dict[str, str] | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Expand every ``mcp_servers`` name list in *spec* into ``mcp_config``.

    Returns a copy — the caller's declaration (which core may replay for
    another provider) is never mutated. An entry that already carries an
    explicit ``mcp_config`` is left alone: a manifest that spells the whole
    thing out has said what it means, and this must not silently overwrite it.
    """
    out = dict(spec)
    for kind in ("agent_configs", "agents"):
        entries = out.get(kind) or []
        if not entries:
            continue
        expanded = []
        for entry in entries:
            names = entry.get("mcp_servers")
            if not names or entry.get("mcp_config"):
                expanded.append(entry)
                continue
            servers = resolve_mcp_servers(
                list(names), url_overrides=url_overrides, config_path=config_path,
            )
            new = {k: v for k, v in entry.items() if k != "mcp_servers"}
            if servers:
                new["mcp_config"] = {"servers": servers}
                # Marks this mcp_config as machine-derived, so _refresh_
                # credentials may re-assert it on an object that already
                # exists. Stripped before the POST by _payload (it is not in
                # any kind's allowed set), so it never reaches the platform.
                new["_mcp_by_reference"] = True
            expanded.append(new)
        out[kind] = expanded
    return out
