"""Migration of the rest of agents-platform-multitenant's ``seed.py`` into
this app's ``contributes.agents`` (18 models, 7 agents, 6 workflows, 1 eval).

The inventory here was checked against the LIVE platform
(``GET /api/{agents,models,workflows}`` + ``GET /api/seeded-state/{kind}/
{slug}``), not against ``seed.py`` itself — six agents and two workflows are
still declared in ``seed.py`` but were deliberately removed from the live
platform (Frederico's own call). Copying ``seed.py`` wholesale would
resurrect them in every new tenant. This file's job is to make that mistake
loud: it fails if any of the six dead agents or two dead workflows ever show
up in this app's manifest again.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]

MIGRATED_MODEL_SLUGS = {
    "claude-cli-sonnet", "claude-cli-opus", "claude-cli-haiku",
    "claude-cli-fable", "claude-cli-readonly", "codex-cli-gpt-5",
    "cursor-agent", "gemini-cli", "github-copilot-cli", "amp-cli",
    "aider-cli", "anthropic-sonnet-4-5", "anthropic-opus-4-1",
    "anthropic-haiku-4-5", "bedrock-sonnet-4-5", "bedrock-opus-4-1",
    "bedrock-nova-pro", "bedrock-llama-3-3",
}
MIGRATED_AGENT_SLUGS = {
    "monitor-shell", "explorer", "planner", "reviewer", "retro",
    "researcher", "coder",
}
MIGRATED_WORKFLOW_SLUGS = {
    "ask-coder", "spec-pipeline", "orchestrator-worker", "parallel-explore",
    "sequential-review", "group-chat-debate",
}
MIGRATED_EVAL_SLUGS = {"explorer-smoke"}

# Deliberately removed from the live platform — must never come back via a
# manifest, no matter how tempting it is to just copy seed.py wholesale.
DEAD_AGENT_SLUGS = {
    "code-builder", "code-enhancer", "app-verifier", "tester",
    "refactorer", "cli-conductor",
}
DEAD_WORKFLOW_SLUGS = {"build-app", "enhance-app"}

# Already owned by other apps — this app must not re-declare them.
OWNED_ELSEWHERE_AGENT_SLUGS = {
    "debugger", "doc-writer",                                  # aw-app-devteam
    "echo-coder", "self-openai-agent", "fake-tool-tester",      # test-fixtures app
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((APP_DIR / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def spec(manifest) -> dict:
    return manifest["contributes"]["agents"]


def test_migrates_exactly_the_18_models(spec):
    slugs = {m["slug"] for m in spec["models"]}
    assert MIGRATED_MODEL_SLUGS <= slugs


def test_migrates_exactly_the_7_agents(spec):
    slugs = {a["slug"] for a in spec["agents"]}
    assert MIGRATED_AGENT_SLUGS <= slugs


def test_migrates_exactly_the_6_workflows(spec):
    slugs = {w["slug"] for w in spec.get("workflows", [])}
    assert slugs == MIGRATED_WORKFLOW_SLUGS


def test_migrates_the_1_eval(spec):
    slugs = {e["slug"] for e in spec.get("evals", [])}
    assert slugs == MIGRATED_EVAL_SLUGS


def test_the_dead_agents_never_appear(spec):
    """These six were removed from the live platform on purpose. Copying
    seed.py wholesale (instead of the vetted inventory) would resurrect
    them in every new tenant — this is the guard that catches that mistake.
    """
    slugs = {a["slug"] for a in spec["agents"]}
    resurrected = slugs & DEAD_AGENT_SLUGS
    assert not resurrected, f"dead agents resurrected in manifest: {resurrected}"


def test_the_dead_workflows_never_appear(spec):
    slugs = {w["slug"] for w in spec.get("workflows", [])}
    resurrected = slugs & DEAD_WORKFLOW_SLUGS
    assert not resurrected, f"dead workflows resurrected in manifest: {resurrected}"


def test_does_not_redeclare_agents_owned_by_other_apps(spec):
    slugs = {a["slug"] for a in spec["agents"]}
    overlap = slugs & OWNED_ELSEWHERE_AGENT_SLUGS
    assert not overlap, f"agents already owned by another app: {overlap}"


def test_no_duplicate_slugs_within_a_kind(spec):
    for kind in ("models", "agents", "workflows", "evals"):
        slugs = [e["slug"] for e in spec.get(kind, [])]
        assert len(slugs) == len(set(slugs)), f"duplicate slug(s) in {kind}"


def test_migrated_agents_reference_files_that_exist(spec):
    for agent in spec["agents"]:
        if agent["slug"] not in MIGRATED_AGENT_SLUGS:
            continue
        ref = agent.get("system_prompt_file")
        if ref is not None:
            assert (APP_DIR / ref).is_file(), f"{agent['slug']} names {ref!r}, missing"


def test_monitor_shell_is_a_prompt_less_attribution_agent(spec):
    """The one migrated agent with no LLM behind it — it exists only so
    Run.source_slug has a real row to describe in the UI (dispatched via
    POST /api/monitor/run, never through the normal agent-run path).
    """
    agents = {a["slug"]: a for a in spec["agents"]}
    monitor = agents["monitor-shell"]
    assert "system_prompt_file" not in monitor
    assert "model_slug" not in monitor


def test_cli_provider_models_bake_in_no_cwd(spec):
    """``cwd`` on a ``cli``-provider Model row is dead weight: agents-platform's
    ``core/executor.py`` unconditionally overwrites ``params["cwd"]`` from the
    dispatching Agent's own permissions before the CLI is ever built, so a
    literal path baked into this static manifest would never take effect —
    and worse, would misleadingly suggest it does.
    """
    for model in spec["models"]:
        if model["slug"] in MIGRATED_MODEL_SLUGS and model["provider"] == "cli":
            assert "cwd" not in model["params"], model["slug"]


def test_eval_target_agent_is_migrated_in_the_same_release(spec):
    """explorer-smoke's target_slug is 'explorer' — one of the 7 migrated
    agents. An eval whose target isn't in the same manifest points at
    nothing on a workspace that only installs this app.
    """
    agent_slugs = {a["slug"] for a in spec["agents"]}
    for ev in spec.get("evals", []):
        if ev["slug"] in MIGRATED_EVAL_SLUGS:
            assert ev["target_kind"] == "agent"
            assert ev["target_slug"] in agent_slugs, ev["slug"]


def test_migrated_workflows_only_reference_agents_in_this_manifest(spec):
    """Every agent slug named by the 6 migrated workflows' graphs must
    resolve within the agents this app itself declares — a dangling
    reference produces a workflow node pointing at nothing on a workspace
    that only installs this app.
    """
    agent_slugs = {a["slug"] for a in spec["agents"]}

    def _agent_refs(graph):
        for node in graph.get("nodes", []):
            yield node["agent"]
        for stage in graph.get("stages", []):
            yield stage["agent"]
        if "orchestrator" in graph:
            yield graph["orchestrator"]["agent"]
        for worker in graph.get("workers", []):
            yield worker["agent"]
        if "synthesizer" in graph:
            yield graph["synthesizer"]["agent"]
        for participant in graph.get("participants", []):
            yield participant["agent"]

    for wf in spec.get("workflows", []):
        if wf["slug"] not in MIGRATED_WORKFLOW_SLUGS:
            continue
        for ref in _agent_refs(wf["graph"]):
            if ref.startswith("workflow:"):
                continue
            assert ref in agent_slugs, f"{wf['slug']} references unknown agent {ref!r}"


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


def test_no_migrated_declaration_ships_a_credential(spec):
    """Same guard the rest of this app's manifest carries — a token pasted
    into a manifest is public the moment the repo is.
    """
    migrated = {
        "models": [m for m in spec["models"] if m["slug"] in MIGRATED_MODEL_SLUGS],
        "agents": [a for a in spec["agents"] if a["slug"] in MIGRATED_AGENT_SLUGS],
        "workflows": [w for w in spec.get("workflows", []) if w["slug"] in MIGRATED_WORKFLOW_SLUGS],
        "evals": [e for e in spec.get("evals", []) if e["slug"] in MIGRATED_EVAL_SLUGS],
    }
    for key_path, value in _walk(migrated):
        leaf = key_path.rsplit(".", 1)[-1].split("[")[0].lower()
        assert leaf not in ("token", "secret", "api_key", "apikey",
                            "password", "authorization"), key_path
        if isinstance(value, str):
            assert not value.lower().startswith("bearer "), key_path
