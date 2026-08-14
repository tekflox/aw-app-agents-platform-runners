"""The provider side of aw-workspace's ``contributes.agents`` surface.

Exercises the whole seed against a stubbed agents-platform — no network, no
running instance. What matters and is asserted here:

* the five kinds are created in dependency order (an Agent's model/config/
  group slugs must already exist; an AgentFlow's graph names agents),
* an existing slug is never POSTed and never updated,
* a 409 is already-there, not a failure,
* an unreachable platform degrades to logs instead of raising into the app
  activation path that calls this.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agents_platform_runners_app.agent_provisioner import AgentProvisioner

SPEC = {
    "models": [{"slug": "sonnet", "provider": "anthropic",
                "model_id": "claude-sonnet-5"}],
    "agent_configs": [{"slug": "rev-cfg", "name": "Reviewer Config"}],
    "groups": [{"slug": "reviewers", "name": "Reviewers",
                "instructions": "Be thorough."}],
    "agents": [{"slug": "sec-reviewer", "name": "Security Reviewer",
                "model_slug": "sonnet", "agent_config_slug": "rev-cfg",
                "group_slug": "reviewers"}],
    "agent_flows": [{"slug": "sec-flow", "name": "Security Flow",
                     "enabled": True, "graph": {
                         "nodes": [
                             {"id": "source", "type": "source", "label": "Source"},
                             {"id": "a", "type": "agent",
                              "agent_slug": "sec-reviewer", "label": "Reviewer"},
                         ],
                         "edges": [{"source": "source", "target": "a"}],
                     }}],
}


class FakePlatform:
    """Minimal stand-in for agents-platform's four create endpoints."""

    def __init__(self, existing=None, post_status=200, list_status=200):
        self.existing = {p: list(v) for p, v in (existing or {}).items()}
        self.post_status = post_status
        self.list_status = list_status
        self.posts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if self.list_status != 200:
                return httpx.Response(self.list_status, text="nope")
            rows = [{"slug": s} for s in self.existing.get(path, [])]
            return httpx.Response(200, json=rows)
        body = json.loads(request.content)
        self.posts.append((path, body))
        if self.post_status != 200:
            return httpx.Response(self.post_status, text="boom")
        return httpx.Response(200, json=body)

    def transport(self):
        return httpx.MockTransport(self.handler)


def _seed(platform, spec=SPEC, **kw):
    p = AgentProvisioner(base="http://ap.test", token="tok",
                         transport=platform.transport(), **kw)
    return p.seed("sec-app", spec)


def test_creates_every_declared_object():
    platform = FakePlatform()
    assert _seed(platform) == {
        "models": 1, "agent_configs": 1, "groups": 1, "agents": 1,
        "agent_flows": 1}


def test_creation_order_puts_the_flow_last_and_the_agent_before_it():
    # A wrong order doesn't error — agents-platform stores model_slug /
    # agent_config_slug / group_slug as plain strings, and an AgentFlow's
    # graph names agents the same way. It just produces objects pointing at
    # things that don't exist. Hence this test.
    platform = FakePlatform()
    _seed(platform)
    assert [path for path, _ in platform.posts] == [
        "/api/models", "/api/agent-configs", "/api/agent-groups",
        "/api/agents", "/api/agent-flows"]


def test_a_flow_keeps_its_graph_and_enabled_flag():
    # The graph is the whole point of a flow, and `enabled` is what makes
    # the platform inject flow context into its member agents at dispatch —
    # a flow seeded with either dropped is a flow that does nothing.
    platform = FakePlatform()
    _seed(platform)
    path, body = platform.posts[-1]
    assert path == "/api/agent-flows"
    assert body["enabled"] is True
    assert [n["id"] for n in body["graph"]["nodes"]] == ["source", "a"]
    assert body["graph"]["edges"] == [{"source": "source", "target": "a"}]


def test_an_existing_slug_is_never_posted():
    platform = FakePlatform(existing={"/api/agents": ["sec-reviewer"]})
    created = _seed(platform)
    assert "agents" not in created
    assert "/api/agents" not in [path for path, _ in platform.posts]


def test_a_409_counts_as_already_there():
    # Created between our GET and our POST — by another workspace seeding the
    # same tenant, or by the user in the UI. Same outcome we wanted.
    platform = FakePlatform(post_status=409)
    assert _seed(platform) == {}


def test_a_server_error_skips_that_object_without_raising():
    platform = FakePlatform(post_status=500)
    assert _seed(platform) == {}


def test_an_unlistable_platform_still_attempts_creates():
    # Falling back to 409-handling is the safe direction: at worst a
    # redundant POST, never a silent update.
    platform = FakePlatform(list_status=500)
    assert _seed(platform)["agents"] == 1


def test_unknown_manifest_fields_are_dropped_before_the_post():
    # agents-platform 422s on an unknown field, which would turn any future
    # manifest-only key into a hard seeding failure for the whole app.
    platform = FakePlatform()
    _seed(platform, {"groups": [{"slug": "g", "name": "G",
                                 "some_future_manifest_key": "x"}]})
    _, body = platform.posts[0]
    assert "some_future_manifest_key" not in body
    assert body["name"] == "G"


def test_a_model_gets_a_display_name_it_did_not_declare():
    platform = FakePlatform()
    _seed(platform, {"models": [{"slug": "sonnet", "provider": "anthropic",
                                 "model_id": "claude-sonnet-5"}]})
    _, body = platform.posts[0]
    assert body["display_name"] == "sonnet"


def test_an_unconfigured_app_seeds_nothing():
    # No token — the app is installed but was never pointed at a platform.
    # Must be a quiet skip, not an exception into the activation path.
    p = AgentProvisioner(base="http://ap.test", token="")
    assert p.seed("sec-app", SPEC) == {}


def test_a_transport_error_does_not_escape():
    def explode(request):
        raise httpx.ConnectError("unreachable", request=request)

    p = AgentProvisioner(base="http://ap.test", token="tok",
                         transport=httpx.MockTransport(explode))
    assert p.seed("sec-app", SPEC) == {}


# --- credential refresh on an object that already exists ---------------------
#
# Seed-once protects what a user tunes. A gateway token is not that: nobody
# types it, and it dies when the gateway rotates. Freezing it at first install
# is what produced agents with a perfect-looking config and zero MCP tools.

REF_SPEC = {"agent_configs": [{"slug": "rev-cfg", "name": "Reviewer Config",
                               "mcp_servers": ["aw-gateway"]}]}
_GW = {"aw-gateway": {"url": "http://gw:9200/mcp",
                      "headers": {"Authorization": "Bearer fresh"}}}


def _seed_with_gateway(platform, spec=REF_SPEC, servers=None):
    import agents_platform_runners_app.agent_provisioner as mod
    orig = mod.resolve_mcp_servers
    mod.resolve_mcp_servers = lambda names, **kw: dict(servers or _GW)
    try:
        return _seed(platform, spec)
    finally:
        mod.resolve_mcp_servers = orig


class RecordingPlatform(FakePlatform):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.puts: list[tuple[str, dict]] = []

    def handler(self, request):
        if request.method == "PUT":
            self.puts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={})
        return super().handler(request)


def test_an_existing_config_gets_its_credentials_refreshed():
    platform = RecordingPlatform(existing={"/api/agent-configs": ["rev-cfg"]})
    _seed_with_gateway(platform)
    assert platform.puts == [("/api/agent-configs/rev-cfg",
                              {"mcp_config": {"servers": _GW}})]


def test_the_refresh_touches_only_mcp_config():
    # A user who retuned a name/description keeps it — we PUT one field.
    platform = RecordingPlatform(existing={"/api/agent-configs": ["rev-cfg"]})
    _seed_with_gateway(platform)
    _, body = platform.puts[0]
    assert list(body) == ["mcp_config"]


def test_a_hand_written_mcp_config_is_never_refreshed():
    # The app spelled it out itself, so it owns it — and a user's hand-edit
    # in the UI has to survive.
    platform = RecordingPlatform(existing={"/api/agent-configs": ["rev-cfg"]})
    _seed(platform, {"agent_configs": [{"slug": "rev-cfg", "name": "R",
                                        "mcp_config": {"servers": {}}}]})
    assert platform.puts == []


def test_the_internal_marker_never_reaches_the_platform():
    # agents-platform 422s on an unknown field.
    platform = RecordingPlatform()
    _seed_with_gateway(platform)
    _, body = platform.posts[0]
    assert "_mcp_by_reference" not in body
    assert body["mcp_config"] == {"servers": _GW}


def test_a_failed_refresh_never_raises():
    class Failing(RecordingPlatform):
        def handler(self, request):
            if request.method == "PUT":
                return httpx.Response(500, text="boom")
            return FakePlatform.handler(self, request)
    _seed_with_gateway(Failing(existing={"/api/agent-configs": ["rev-cfg"]}))
