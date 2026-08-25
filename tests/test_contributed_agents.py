"""This app is the first one to both PROVIDE the agent-contribution surface
and USE it.

``plugin.py::register_contributed_agents`` is the provider side of
aw-workspace's protocol — every other app's ``contributes.agents`` is seeded
by *this* app. Declaring agents here means core hands this app's own
declaration back to this app's own provider during its own activation. That
works (the registry looks the provider up by protocol, not by app id, and a
declaration arriving before a provider is held and replayed), but it is a
path nothing exercised until 2026-08-21 — so the shape is pinned here rather
than left to be rediscovered by whoever breaks it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]

#: This file only pins the Telegram family's own shape. The manifest has
#: since grown other agent families (see test_seed_migration.py) that don't
#: share this family's MCP-gateway contract — skill_slugs/agent_config_slug
#: are how the Telegram agents reach tools, not a rule every agent follows.
TELEGRAM_AGENT_SLUGS = {"telegram-sonnet", "telegram-opus", "telegram-haiku",
                        "telegram-fable", "telegram-gpt-5-6-sol"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((APP_DIR / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def spec(manifest) -> dict:
    return manifest["contributes"]["agents"]


def test_declares_the_capability_the_contribution_needs(manifest):
    # Core rejects contributes.agents without it — the install fails rather
    # than silently dropping the declaration. Easy to miss here because this
    # app already had six other permissions before it ever contributed one.
    assert "agents:contribute" in manifest["permissions"]


def test_ships_the_telegram_family(spec):
    """Five model variants of one role, same contract, chosen by what the
    conversation is worth — the same shape as the Coder and QA families.

    They were platform-seeded rows until now: a workspace that installed no
    apps still got them, and a workspace whose platform stopped seeding would
    have lost its entire Telegram channel with nothing to say why.
    """
    slugs = {a["slug"] for a in spec["agents"]}
    assert TELEGRAM_AGENT_SLUGS <= slugs


def test_declares_gpt_5_6_sol_for_the_codex_runner(spec):
    models = {model["slug"]: model for model in spec["models"]}
    model = models["codex-runner-gpt-5-6-sol"]
    assert model["provider"] == "runner"
    assert model["params"] == {
        "runner": "aw-codex",
        "cli": "codex",
        "model": "gpt-5.6-sol",
        "dangerous_skip_permissions": True,
        "timeout_s": 900,
    }


def test_every_agent_uses_the_shared_contract_and_prompt(spec):
    """One prompt file for all five. The live rows were byte-identical across
    the family before this app adopted them, and the contract that actually
    matters is the aw-agent-telegram skill — the prompt is a pointer to it.
    """
    for agent in spec["agents"]:
        if agent["slug"] not in TELEGRAM_AGENT_SLUGS:
            continue
        assert agent["skill_slugs"] == ["aw-agent-telegram"], agent["slug"]
        assert agent["system_prompt_file"] == "prompts/telegram.md", agent["slug"]


def test_the_skill_every_declared_agent_names_is_shipped_by_this_app(manifest, spec):
    """No dependency hop for these: this app ships aw-agent-telegram itself,
    so the agents and their contract install or fail together.
    """
    shipped = {s["id"] for s in manifest["contributes"]["skills"]}
    for agent in spec["agents"]:
        for slug in agent.get("skill_slugs") or []:
            assert slug in shipped, f"{agent['slug']} names {slug!r}, unshipped"


def test_referenced_files_exist(spec):
    for agent in spec["agents"]:
        ref = agent.get("system_prompt_file")
        if ref is not None:
            assert (APP_DIR / ref).is_file(), agent["slug"]


def test_every_agent_names_a_config_this_app_declares(spec):
    """`agent-config-aw-full` is also declared by aw-app-mobile, and that
    duplication is deliberate rather than an accident to clean up.

    Seeding is create-if-absent by slug, so two apps declaring the same
    config is harmless — whichever installs first creates it. Declaring it
    here is what makes THIS app installable on its own: pointing at a slug
    only aw-app-mobile ships would mean a workspace that wants Telegram but
    not the mobile app gets four agents referencing a config that does not
    exist, and a dangling agent_config_slug does not error — it produces an
    agent with no MCP surface, which reads as a broken model.

    If the two ever need to differ, the answer is two slugs, not one shared
    row edited by hand.
    """
    configs = {c["slug"] for c in spec["agent_configs"]}
    assert configs == {"agent-config-aw-full"}
    for agent in spec["agents"]:
        if agent["slug"] not in TELEGRAM_AGENT_SLUGS:
            continue
        assert agent["agent_config_slug"] in configs, agent["slug"]


def test_the_config_is_declared_by_reference_not_by_url(spec):
    """`mcp_servers: ["aw-gateway"]` makes the provisioner resolve the live
    URL and token from this workspace's own .mcp.json at activation. An
    inline connection dict would commit a credential and freeze an address
    — see the mcp_url_overrides comment in plugin.py for the incident that
    rule came from.
    """
    cfg = spec["agent_configs"][0]
    assert cfg["mcp_servers"] == ["aw-gateway"]
    assert "mcp_config" not in cfg
    blob = json.dumps(cfg)
    assert "http" not in blob, "a config declared by reference names no URL"


def _walk(value, path=""):
    """Every (key-path, scalar) pair in a nested declaration."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def test_no_declaration_ships_a_credential(spec):
    """Same guard the other contributing apps carry — a token pasted into a
    manifest is public the moment the repo is.

    Checks KEYS, not a substring of the whole blob. The first version of this
    test searched the serialized declaration for the word "token" and failed
    on the config's own description, which says the token is resolved at
    activation and deliberately not committed — i.e. it flagged the sentence
    explaining why there is no credential. Prose is where you explain a rule;
    a key named `token` is where you break it.
    """
    for key_path, value in _walk(spec):
        leaf = key_path.rsplit(".", 1)[-1].split("[")[0].lower()
        assert leaf not in ("token", "secret", "api_key", "apikey",
                            "password", "authorization"), key_path
        if isinstance(value, str):
            assert not value.lower().startswith("bearer "), key_path
