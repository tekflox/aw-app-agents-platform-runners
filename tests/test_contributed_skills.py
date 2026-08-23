"""Every skill this app declares must actually ship — and one of them is
load-bearing for agents-platform itself.

agents-platform's executor auto-injects `aw-agents-flow` into any agent that
is a node in an ENABLED Agents Flow (core/executor.py::_agents_flow_context,
via load_skill). It reads the workspace skills tree, which is populated
purely from installed apps' contributes.skills — so if no app ships that
slug, load_skill returns None, the injection silently degrades to the bare
adjacency list, and the agent never learns the three terminal actions it is
required to end its turn with. It then gets reprompted, fails to decide
again, and every flow run ends in a "🆘 Agents Flow needs a human"
escalation. Nothing in that chain reports a missing file.

That is exactly what happened until 2026-08-14: the skill existed only in
the agentic-workspace monolith and had never been ported.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((APP_DIR / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def declared(manifest) -> dict[str, str]:
    return {s["id"]: s["path"] for s in manifest["contributes"]["skills"]}


def test_every_declared_skill_file_exists(declared):
    for slug, rel in declared.items():
        assert (APP_DIR / rel).is_file(), f"{slug} declares {rel}, which is not in the repo"


def test_the_skill_dir_and_the_manifest_agree(declared):
    """A SKILL.md in the tree that nobody declares never reaches an agent."""
    on_disk = {p.parent.name for p in (APP_DIR / "skills").glob("*/SKILL.md")}
    assert on_disk == set(declared), (
        f"declared-but-missing={set(declared) - on_disk}, "
        f"present-but-undeclared={on_disk - set(declared)}"
    )


def test_the_agents_flow_contract_is_shipped(declared):
    """The one agents-platform loads by slug, with no fallback."""
    assert "aw-agents-flow" in declared


def test_the_agents_flow_skill_still_teaches_the_three_terminal_actions(declared):
    """The whole point of injecting it. An agent that ends a turn without one
    of these is what the need-human escalation fires on."""
    text = (APP_DIR / declared["aw-agents-flow"]).read_text()
    for action in ("run_agent_async", "return_to_caller_agent", "mark_flow_done"):
        assert action in text, f"aw-agents-flow no longer mentions {action}"


def test_the_flow_contract_requires_an_explicit_run_id(declared):
    """A continuing chat may receive the skill text without a platform run.

    Calling a terminal action there can only fail with "Could not identify
    this run" and leaks internal bookkeeping into the user's conversation.
    The contract must make absence of an explicit run ID a stop condition,
    not an invitation to guess or hunt for one.
    """
    text = (APP_DIR / declared["aw-agents-flow"]).read_text()
    assert "explicit Agents Platform run ID" in text
    assert "do not search for, infer, or fabricate a run ID" in text
    assert "No terminal action is required in that case" in text


def test_skills_carry_a_frontmatter_name_matching_their_slug(declared):
    # The slug agents-platform loads by is the directory name; a mismatched
    # frontmatter name makes the skill un-findable by search_skills.
    for slug, rel in declared.items():
        head = (APP_DIR / rel).read_text()[:400]
        m = re.search(r"^name:\s*(\S+)\s*$", head, re.MULTILINE)
        assert m, f"{slug} has no frontmatter name"
        assert m.group(1) == slug, f"{slug} declares name: {m.group(1)}"


def test_no_skill_points_at_a_tool_namespace_that_does_not_exist(declared):
    """These skills are read by agents that then look the names up.

    `agent-mcp` and `agents_platform__<tool>` are pre-gateway spellings; the
    tools arrive as `aw__agents_platform_runners__<tool>`. A skill naming the
    old form teaches an agent to conclude the tools are missing — and
    aw-agents' rule 2 tells it to STOP when it concludes that.
    """
    stale = ("mcp__agent-mcp__", "`agent-mcp`", "aw-gateway__agents_platform__",
             "aw-knowledge-base")
    for slug, rel in declared.items():
        text = (APP_DIR / rel).read_text()
        for marker in stale:
            assert marker not in text, f"{slug} still references {marker!r}"


def test_the_flow_contract_separates_a_failed_task_from_a_failed_tool_call(declared):
    """The distinction a live incident cost a card to learn (2026-08-21).

    A QA agent finished its review, called set_qa_status successfully, then
    hit connection errors on mark_flow_done and a timeout on
    return_to_caller_agent — both on one flaky gateway upstream. Following
    its "don't hunt for workarounds, flag it" rule it called set_blocker,
    which moved a card that had just PASSED review to Need Human. The agent
    obeyed its contract; the contract had nothing to say about a terminal
    action that fails to execute, as opposed to work that fails.

    The runtime already handles a turn that ends without a terminal action —
    it reprompts once (a free retry after the upstream recovers) and then
    escalates with flow_needs_human plus a sysadmin ping carrying the run
    id. That path is strictly better than any card-status improvisation, so
    the contract has to point at it by name.
    """
    text = (APP_DIR / declared["aw-agents-flow"]).read_text()
    assert "When the terminal action itself FAILS to execute" in text
    # Naming the runtime functions is what makes the claim checkable by
    # whoever doubts it, instead of another rule to take on faith.
    assert "_escalate_need_human" in text
    assert "reprompt" in text.lower()
    # The prohibition has to be explicit about BOTH ways an agent can reach
    # for the card — set_blocker is the one that fired, move_kanban_task is
    # the obvious next improvisation.
    assert "set_blocker" in text and "need_human" in text


def test_the_qa_contract_scopes_set_blocker_to_the_review(declared):
    """aw-agent-qa's "call set_blocker immediately if you're stuck" is the
    rule that got misapplied — correctly read, wrongly scoped. It has to say
    that "stuck" means the review is stuck, not that a tool call after the
    verdict failed.
    """
    text = (APP_DIR / declared["aw-agent-qa"]).read_text()
    assert "not your own bookkeeping" in text.lower()
    assert "mark_flow_done" in text
