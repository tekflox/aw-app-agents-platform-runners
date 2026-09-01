"""``mcp_servers`` by reference — how a contributed agent gets the gateway.

An app cannot declare the aw-gateway MCP entry directly: it is
``{url, headers: {Authorization: Bearer <token>}}`` and a manifest is a
public artefact that ships to a marketplace. So an app declares the NAME,
and this side resolves it against the workspace's own ``.mcp.json`` at seed
time — the intention travels, the credential does not.

Run: python3 -m pytest -c /dev/null tests/test_mcp_references.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import agent_provisioner as ap  # noqa: E402

TOKEN = "Bearer s3cr3t-gateway-token"


def _mcp_json(tmp_path, servers=None) -> str:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers if servers is not None else {
        "aw-gateway": {
            "type": "http",
            "url": "http://aw-app-mcp-gateway:9200/mcp",
            "headers": {"Authorization": TOKEN},
        },
    }}))
    return str(path)


def _spec(**cfg_overrides) -> dict:
    cfg = {"slug": "c1", "name": "C1", "mcp_servers": ["aw-gateway"]}
    cfg.update(cfg_overrides)
    return {"agent_configs": [cfg], "agents": [{"slug": "a1", "name": "A1"}]}


# --- resolution --------------------------------------------------------------


def test_a_named_server_resolves_to_url_and_headers(tmp_path):
    got = ap.resolve_mcp_servers(["aw-gateway"], config_path=_mcp_json(tmp_path))
    assert got["aw-gateway"]["url"] == "http://aw-app-mcp-gateway:9200/mcp"
    assert got["aw-gateway"]["headers"] == {"Authorization": TOKEN}


def test_url_override_replaces_the_address_but_keeps_the_token(tmp_path):
    """The address in .mcp.json is this container's view of the gateway.

    A spawned agent container is a sibling in the nested podman namespace and
    cannot resolve the compose name — same gateway, same token, different
    address. Getting this backwards yields an agent that authenticates fine
    against a host that does not exist.
    """
    got = ap.resolve_mcp_servers(
        ["aw-gateway"], config_path=_mcp_json(tmp_path),
        url_overrides={"aw-gateway": "http://172.18.0.1:9200/mcp"},
    )
    assert got["aw-gateway"]["url"] == "http://172.18.0.1:9200/mcp"
    assert got["aw-gateway"]["headers"] == {"Authorization": TOKEN}


def test_an_unknown_server_name_is_skipped_not_raised(tmp_path):
    got = ap.resolve_mcp_servers(["nope"], config_path=_mcp_json(tmp_path))
    assert got == {}


def test_a_missing_mcp_json_degrades_instead_of_failing_the_install(tmp_path):
    # An app that installs without its gateway is degraded; an app that
    # refuses to install is broken. Prefer degraded.
    got = ap.resolve_mcp_servers(["aw-gateway"],
                                 config_path=str(tmp_path / "absent.json"))
    assert got == {}


def test_an_entry_with_no_url_is_skipped(tmp_path):
    path = _mcp_json(tmp_path, servers={"aw-gateway": {"type": "http"}})
    assert ap.resolve_mcp_servers(["aw-gateway"], config_path=path) == {}


# --- scoped profiles ---------------------------------------------------------
#
# The dict form names a SCOPED slice of the gateway (one of its own named
# configs) without putting the bearer token in a public manifest: the server
# entry is resolved exactly as above and the profile is appended to its path.


def test_a_profile_is_appended_to_the_server_s_path(tmp_path):
    got = ap.resolve_mcp_servers(
        [{"name": "crispal", "server": "aw-gateway", "profile": "crispal-full"}],
        config_path=_mcp_json(tmp_path),
    )
    assert got["crispal"]["url"] == "http://aw-app-mcp-gateway:9200/mcp/crispal-full"
    assert got["crispal"]["headers"] == {"Authorization": TOKEN}


def test_the_resolved_key_is_the_local_name_not_the_server_name(tmp_path):
    """Renaming this key renames every tool the agent sees (mcp__<key>__*)."""
    got = ap.resolve_mcp_servers(
        [{"name": "crispal", "server": "aw-gateway", "profile": "crispal-full"}],
        config_path=_mcp_json(tmp_path),
    )
    assert list(got) == ["crispal"]


def test_the_key_falls_back_to_the_profile_then_the_server(tmp_path):
    path = _mcp_json(tmp_path)
    assert list(ap.resolve_mcp_servers(
        [{"server": "aw-gateway", "profile": "crispal-full"}], config_path=path)) \
        == ["crispal-full"]
    assert list(ap.resolve_mcp_servers([{"server": "aw-gateway"}], config_path=path)) \
        == ["aw-gateway"]


def test_server_defaults_to_the_gateway(tmp_path):
    got = ap.resolve_mcp_servers([{"name": "c", "profile": "crispal-full"}],
                                 config_path=_mcp_json(tmp_path))
    assert got["c"]["url"] == "http://aw-app-mcp-gateway:9200/mcp/crispal-full"


def test_a_dict_without_a_profile_matches_the_string_form(tmp_path):
    path = _mcp_json(tmp_path)
    assert ap.resolve_mcp_servers([{"server": "aw-gateway"}], config_path=path) \
        == ap.resolve_mcp_servers(["aw-gateway"], config_path=path)


def test_the_url_override_applies_to_the_base_and_the_profile_follows(tmp_path):
    """One knob still moves every by-reference config, scoped or not."""
    got = ap.resolve_mcp_servers(
        [{"name": "crispal", "server": "aw-gateway", "profile": "crispal-full"}],
        config_path=_mcp_json(tmp_path),
        url_overrides={"aw-gateway": "http://172.18.0.1:9200/mcp"},
    )
    assert got["crispal"]["url"] == "http://172.18.0.1:9200/mcp/crispal-full"


def test_a_trailing_slash_on_the_base_does_not_double_up(tmp_path):
    path = _mcp_json(tmp_path, servers={"aw-gateway": {
        "type": "http", "url": "http://aw-app-mcp-gateway:9200/mcp/"}})
    got = ap.resolve_mcp_servers([{"name": "c", "profile": "crispal-full"}],
                                 config_path=path)
    assert got["c"]["url"] == "http://aw-app-mcp-gateway:9200/mcp/crispal-full"


def test_a_scoped_entry_with_an_unknown_server_is_skipped_not_raised(tmp_path):
    got = ap.resolve_mcp_servers([{"name": "c", "server": "nope", "profile": "p"}],
                                 config_path=_mcp_json(tmp_path))
    assert got == {}


def test_a_malformed_entry_is_skipped_not_raised(tmp_path):
    path = _mcp_json(tmp_path)
    assert ap.resolve_mcp_servers([None, 7, "", ["aw-gateway"]], config_path=path) == {}
    # An empty dict is not malformed — every field defaults, so it means the
    # gateway, unscoped, exactly like the string form.
    assert list(ap.resolve_mcp_servers([{}], config_path=path)) == ["aw-gateway"]


def test_a_scoped_expansion_is_still_marked_by_reference(tmp_path):
    """The whole point of the card: _refresh_credentials re-asserts it, so a
    hard-delete + reseed converges on the right scope instead of the wrong one."""
    out = ap.apply_mcp_references(
        _spec(mcp_servers=[{"name": "crispal", "server": "aw-gateway",
                            "profile": "crispal-full"}]),
        config_path=_mcp_json(tmp_path),
    )
    cfg = out["agent_configs"][0]
    assert cfg["_mcp_by_reference"] is True
    assert cfg["_mcp_scoped"] == ["crispal"]
    assert cfg["mcp_config"]["servers"]["crispal"]["url"].endswith("/mcp/crispal-full")


def test_an_unscoped_expansion_carries_no_scoped_marker(tmp_path):
    out = ap.apply_mcp_references(_spec(), config_path=_mcp_json(tmp_path))
    assert "_mcp_scoped" not in out["agent_configs"][0]


def test_the_probe_never_raises_on_an_unreachable_profile(tmp_path):
    """A seed must survive a gateway that is down, a DNS name that does not
    resolve, or anything else the probe trips over."""
    spec = ap.apply_mcp_references(
        _spec(mcp_servers=[{"name": "crispal", "profile": "crispal-full"}]),
        config_path=_mcp_json(tmp_path),
        url_overrides={"aw-gateway": "http://127.0.0.1:1/mcp"},
    )
    ap.probe_scoped_profiles(spec, timeout=0.05)


def test_the_probe_ignores_unscoped_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(ap.httpx, "post", lambda *a, **k: pytest.fail("probed"))
    ap.probe_scoped_profiles(ap.apply_mcp_references(
        _spec(), config_path=_mcp_json(tmp_path)))


# --- spec expansion ----------------------------------------------------------


def test_expansion_replaces_the_name_list_with_a_real_mcp_config(tmp_path):
    out = ap.apply_mcp_references(_spec(), config_path=_mcp_json(tmp_path))
    cfg = out["agent_configs"][0]
    assert "mcp_servers" not in cfg, "the reference must not reach the platform"
    assert cfg["mcp_config"]["servers"]["aw-gateway"]["headers"] == {"Authorization": TOKEN}


def test_expansion_does_not_mutate_the_caller_s_declaration(tmp_path):
    # core may replay the same declaration for another provider.
    spec = _spec()
    ap.apply_mcp_references(spec, config_path=_mcp_json(tmp_path))
    assert spec["agent_configs"][0]["mcp_servers"] == ["aw-gateway"]


def test_an_explicit_mcp_config_is_left_alone(tmp_path):
    explicit = {"servers": {"custom": {"url": "http://example/mcp"}}}
    out = ap.apply_mcp_references(
        _spec(mcp_config=explicit), config_path=_mcp_json(tmp_path))
    assert out["agent_configs"][0]["mcp_config"] == explicit


def test_unresolvable_reference_leaves_no_empty_mcp_config(tmp_path):
    """Seeding ``mcp_config: {"servers": {}}`` would look configured.

    An absent key is honest about the gap; an empty dict reads as "someone
    set this up" to the next person opening the Agent Config.
    """
    out = ap.apply_mcp_references(
        _spec(mcp_servers=["nope"]), config_path=_mcp_json(tmp_path))
    assert "mcp_config" not in out["agent_configs"][0]
    assert "mcp_servers" not in out["agent_configs"][0]


def test_entries_without_references_pass_through_untouched(tmp_path):
    spec = {"agent_configs": [{"slug": "c", "name": "C"}], "models": [{"slug": "m"}]}
    out = ap.apply_mcp_references(spec, config_path=_mcp_json(tmp_path))
    assert out["agent_configs"][0] == {"slug": "c", "name": "C"}
    assert out["models"] == [{"slug": "m"}]


def test_agents_may_carry_references_too(tmp_path):
    spec = {"agents": [{"slug": "a", "name": "A", "mcp_servers": ["aw-gateway"]}]}
    out = ap.apply_mcp_references(spec, config_path=_mcp_json(tmp_path))
    assert out["agents"][0]["mcp_config"]["servers"]["aw-gateway"]["url"]


def test_seed_expands_references_before_posting(tmp_path, monkeypatch):
    """End-to-end through seed(): what actually reaches the platform."""
    monkeypatch.setattr(ap, "MCP_CONFIG_PATH", _mcp_json(tmp_path))
    posted: list[tuple[str, dict]] = []

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return []

        def raise_for_status(self):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            return FakeResp()

        def post(self, path, json):
            posted.append((path, json))
            return FakeResp()

    monkeypatch.setattr(ap.httpx, "Client", lambda **kw: FakeClient())

    prov = ap.AgentProvisioner(base="http://ap", token="t",
                               mcp_url_overrides={"aw-gateway": "http://172.18.0.1:9200/mcp"})
    prov.seed("maintenance-agents", _spec())

    cfg_body = next(b for p, b in posted if p == "/api/agent-configs")
    assert cfg_body["mcp_config"]["servers"]["aw-gateway"] == {
        "type": "http",
        "url": "http://172.18.0.1:9200/mcp",
        "headers": {"Authorization": TOKEN},
    }
    assert "mcp_servers" not in cfg_body
