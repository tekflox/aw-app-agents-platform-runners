"""The provider side of aw-workspace's ``contributes.agents`` surface.

Exercises the whole seed against a stubbed agents-platform — no network, no
running instance. What matters and is asserted here:

* the four kinds are created in dependency order (an Agent's model/config/
  group slugs must already exist),
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
        "models": 1, "agent_configs": 1, "groups": 1, "agents": 1}


def test_creation_order_puts_the_agent_last():
    # A wrong order doesn't error — agents-platform stores model_slug /
    # agent_config_slug / group_slug as plain strings. It just produces an
    # agent pointing at three things that don't exist. Hence this test.
    platform = FakePlatform()
    _seed(platform)
    assert [path for path, _ in platform.posts] == [
        "/api/models", "/api/agent-configs", "/api/agent-groups", "/api/agents"]


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
