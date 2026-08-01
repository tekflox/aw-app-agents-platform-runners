"""agent-mcp — control the local Agents Platform from any MCP-aware client.

A **stdio** MCP server that exposes the platform's agents, workflows and runs
to a calling model (Claude Code, etc.).

What this gives the model:
  * Static control-plane tools: ``list_*``, ``get_*``, ``create_*``,
    ``update_*``, ``delete_*``, ``restore_*`` — full CRUD for agents and
    workflows. Delete is **soft** by default (recoverable) — hard delete
    requires ``hard=true``.
  * Dynamic per-resource runners: ``agent_<slug>`` / ``workflow_<slug>``
    appear automatically for every active (non-deleted) agent / workflow.
  * Run inspection: ``run_async`` (fire-and-forget), ``run_status`` (poll),
    ``run_events`` (event stream), ``cancel_run``, ``cancel_all_runs``.
  * Budget / safety: workflows can carry ``graph.max_hops`` and
    ``graph.max_tokens`` — exceeding either stops the run gracefully with
    ``output.limit_reached`` set.

If the backend isn't already running on ``$AGENTS_BASE`` (default
``http://127.0.0.1:8765``) the first tool call starts it.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Hand-rolled JSON-RPC stdio, not the `mcp` SDK: this module is spawned by
# aw-mcp-gateway (aw-app-mcp-gateway) as a stdio upstream, whose own
# container has no `mcp` package installed — every other src/mcp/*.py
# server in this project already avoids the SDK for the same reason (see
# aw_lifestyle.py's docstring). `Server`/`stdio_server`/`Tool`/`TextContent`
# below are minimal drop-in stand-ins for just the surface this file
# actually uses, so porting from the original agents-platform/mcp_server/
# agent_mcp.py (SDK-based) needed zero changes below this point — every
# `@server.list_tools()`/`@server.call_tool()` decorator, every
# `Tool(...)`/`TextContent(...)` construction, is the SDK's exact
# constructor shape, just backed by a plain class instead of a real
# `mcp.types` model.


class Tool:
    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class TextContent:
    def __init__(self, type: str, text: str):
        self.type = type
        self.text = text


class _NoopServer:
    """Stand-in for mcp.server.Server — its decorators just register a
    handler in the real SDK; here they're identity (no-ops), so
    `_list_tools`/`_call_tool` below stay plain, directly-callable
    functions that main() below calls straight instead of going through
    a Server/stdio_server transport loop."""

    def __init__(self, *_a, **_kw):
        pass

    def list_tools(self):
        return lambda f: f

    def call_tool(self):
        return lambda f: f


BASE = os.environ.get("AGENTS_BASE", "http://127.0.0.1:8765")

INSTRUCTIONS = """\
You are connected to the **Agents Platform** — a local control plane for
defining, running and observing AI agents + multi-agent workflows.

## Discovery flow

1. Call ``list_agents`` and ``list_workflows`` first. Each row carries a
   slug, name, description, and (for workflows) the ``kind`` + ``graph``.
2. To execute a known resource use the dynamic tools ``agent_<slug>`` or
   ``workflow_<slug>`` — those are generated per-row and block until done.
3. For long-running work prefer ``run_workflow_async`` / ``run_agent_async``
   + ``run_status`` polling so you can stay responsive.

## CRUD flow

* ``create_agent`` / ``create_workflow`` accept a full spec.
* ``update_agent`` / ``update_workflow`` accept a partial patch — only the
  fields you send are changed.
* ``delete_agent`` / ``delete_workflow`` default to **soft-delete** (sets
  ``deleted_at``, hidden from default lists, recoverable). Pass
  ``hard=true`` for an irreversible delete.
* ``restore_agent`` / ``restore_workflow`` undo a soft-delete.
* ``list_*_deleted`` shows the trash bin.

## Budget / safety

Every workflow can declare ``graph.max_hops`` (default 50) and
``graph.max_tokens`` (default unlimited). Hitting either stops the run
gracefully — status remains ``success`` but ``output.limit_reached`` is set
to ``"hops"`` or ``"tokens"`` and ``output.<kind>_limit_reached`` is true.

## Examples

Create a workflow that runs two agents sequentially with a hop cap:

```json
{
  "name": "create_workflow",
  "arguments": {
    "slug": "plan-then-build",
    "name": "Plan → Build",
    "description": "Planner outlines the work, then coder implements.",
    "kind": "sequential",
    "graph": {
      "concurrency": "sequential",
      "max_hops": 20,
      "max_tokens": 100000,
      "nodes": [
        {"id": "plan",  "agent": "planner", "label": "Plan",
         "input_template": "{input}"},
        {"id": "build", "agent": "coder",   "label": "Build",
         "input_template": "Plan:\\n{prev}\\n\\nNow implement it."}
      ]
    }
  }
}
```

Run it and wait for the result:

```json
{"name": "workflow_plan_then_build", "arguments": {"input": "Build a tic-tac-toe game"}}
```

Or start it in the background and poll:

```json
{"name": "run_workflow_async", "arguments": {"slug": "plan-then-build", "input": "..."}}
{"name": "run_status",         "arguments": {"run_id": "<id>"}}
```
"""


# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------

def _running() -> bool:
    try:
        return httpx.get(f"{BASE}/api/health", timeout=1.5).status_code == 200
    except Exception:
        return False


def _ensure_running() -> bool:
    if _running():
        return True
    repo = Path(__file__).resolve().parents[1]
    log = repo / "data" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    venv_py = repo / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    subprocess.Popen(
        [py, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
         "--port", str(BASE.rsplit(":", 1)[-1].rstrip("/")), "--log-level", "warning"],
        cwd=str(repo), stdout=open(log, "ab"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(50):
        if _running():
            return True
        time.sleep(0.2)
    return False


server = _NoopServer("agent-mcp", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

def _agent_spec_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "slug":          {"type": "string", "description": "Unique URL-safe id (lowercase, dashes)."},
            "name":          {"type": "string"},
            "description":   {"type": "string", "description": "One-line summary of what this agent does."},
            "system_prompt": {"type": "string", "description": "Free-form system instructions for the LLM."},
            "use_cases":     {"type": "array", "items": {"type": "string"},
                              "description": "Short list of example use cases — when to pick this agent. e.g. ['Greenfield product research', 'Domain investigation before plan']. Consumed by the conductor to choose between agents."},
            "model_slug":    {"type": ["string", "null"],
                              "description": "Slug from list_models; omit to use platform default."},
            "tool_specs":    {"type": "array", "items": {"type": "string"},
                              "description": "Tool ids like 'code.read_file', 'code.write_file'."},
            "skill_slugs":   {"type": "array", "items": {"type": "string"}},
            "params":        {"type": "object",
                              "description": "Model params like {\"temperature\":0.2,\"max_tokens\":2000}."},
            "icon":          {"type": "string"},
            "color":         {"type": "string"},
        },
        "required": ["slug", "name"],
    }


def _agent_patch_schema() -> dict:
    s = dict(_agent_spec_schema())
    s["required"] = ["slug"]
    s["properties"] = dict(s["properties"])
    s["properties"]["slug"] = {"type": "string",
                               "description": "Slug of the agent to patch."}
    return s


def _workflow_spec_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "slug":        {"type": "string"},
            "name":        {"type": "string"},
            "description": {"type": "string"},
            "use_cases":   {"type": "array", "items": {"type": "string"},
                            "description": "Short list of example use cases — when to pick this workflow. e.g. ['Build new product with research', 'Investigate cross-cutting bug']. Consumed by the conductor to choose between workflows."},
            "kind":        {"type": "string",
                            "enum": ["sequential", "parallel", "pipeline",
                                     "orchestrator_worker", "group_chat"],
                            "description": "Derived from graph shape if omitted."},
            "graph":       {"type": "object",
                            "description": ("Topology + nodes. Shapes: "
                                "sequential/parallel → {nodes:[…], concurrency:'sequential|parallel'}, "
                                "pipeline → {stages:[…]}, "
                                "orchestrator_worker → {orchestrator,workers,synthesizer}, "
                                "group_chat → {participants,max_turns}. "
                                "Add graph.max_hops / graph.max_tokens for budget caps. "
                                "A node's 'agent' may be 'workflow:<slug>' to invoke a sub-workflow.")},
        },
        "required": ["slug", "name", "graph"],
    }


def _workflow_patch_schema() -> dict:
    s = dict(_workflow_spec_schema())
    s["required"] = ["slug"]
    s["properties"] = dict(s["properties"])
    s["properties"]["slug"] = {"type": "string",
                               "description": "Slug of the workflow to patch."}
    return s


def _target_spec_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "slug":              {"type": "string", "description": "Unique URL-safe id (e.g. 'us1924311-acsb-alerts')."},
            "name":              {"type": "string", "description": "Human-readable goal name."},
            "description":       {"type": "string", "description": "What this Target is delivering. Multi-line OK."},
            "source_kind":       {"type": "string", "enum": ["manual", "rally_story", "incident", "jira", "github_issue", "github_pr", "loop", "other"],
                                  "description": "Where the goal originated."},
            "source_ref":        {"type": ["string", "null"], "description": "Identifier in the source system (US1924311, INC-123, github URL, ...)."},
            "plan_canvas_id":    {"type": ["string", "null"], "description": "Canvas id for the approved plan, if any."},
            "report_canvas_id":  {"type": ["string", "null"], "description": "Canvas id for the final report, if any."},
            "budget_tokens":     {"type": ["integer", "null"]},
            "budget_usd":        {"type": ["number", "null"]},
            "tags":              {"type": "array", "items": {"type": "string"}},
            "notes":             {"type": "string"},
            "created_by":        {"type": ["string", "null"]},
        },
        "required": ["slug", "name"],
    }


def _target_patch_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "slug":              {"type": "string", "description": "Slug of the Target to patch."},
            "name":              {"type": ["string", "null"]},
            "description":       {"type": ["string", "null"]},
            "source_kind":       {"type": ["string", "null"]},
            "source_ref":        {"type": ["string", "null"]},
            "plan_canvas_id":    {"type": ["string", "null"]},
            "report_canvas_id":  {"type": ["string", "null"]},
            "budget_tokens":     {"type": ["integer", "null"]},
            "budget_usd":        {"type": ["number", "null"]},
            "status":            {"type": ["string", "null"], "enum": [None, "active", "completed", "cancelled", "abandoned"]},
            "tags":              {"type": ["array", "null"], "items": {"type": "string"}},
            "notes":             {"type": ["string", "null"]},
        },
        "required": ["slug"],
    }


@server.list_tools()
async def _list_tools() -> list[Tool]:
    _ensure_running()
    async with httpx.AsyncClient(timeout=10) as c:
        agents = (await c.get(f"{BASE}/api/agents")).json()
        workflows = (await c.get(f"{BASE}/api/workflows")).json()

    static: list[Tool] = [
        # ----- discovery -----
        Tool(name="list_agents",
             description=("List active (non-deleted) agents. Each row includes "
                          "slug, name, description, system_prompt, **use_cases** "
                          "(short examples of when to pick this agent), model_slug, "
                          "tool_specs, skill_slugs. Use this to PICK an agent for a "
                          "task — read use_cases first, then description, then "
                          "system_prompt for the full spec.\n\n"
                          "Pass `exclude_pattern` (SQL LIKE) to hide clutter, "
                          "e.g. `{\"exclude_pattern\":\"agent-ui-%\"}` to drop "
                          "playwright-test rows.\n\n"
                          "Example: `{\"name\":\"list_agents\"}`."),
             inputSchema={"type": "object", "properties": {
                 "include_deleted": {"type": "boolean",
                                     "description": "Set true to include soft-deleted rows."},
                 "exclude_pattern": {"type": "string",
                                     "description": "SQL LIKE pattern to drop matching slugs (use % wildcards). E.g. 'agent-ui-%'."},
             }}),
        Tool(name="list_workflows",
             description=("List active (non-deleted) workflows. Each row includes "
                          "slug, name, description, **use_cases** (short examples of "
                          "when to pick this workflow), kind, and the full graph. "
                          "Use this to PICK a workflow for a task — read use_cases "
                          "first, then description.\n\n"
                          "Pass `exclude_pattern` (SQL LIKE) to hide clutter."),
             inputSchema={"type": "object", "properties": {
                 "include_deleted": {"type": "boolean"},
                 "exclude_pattern": {"type": "string"},
             }}),
        Tool(name="list_agents_deleted",
             description="List ONLY soft-deleted agents (the trash bin).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="list_workflows_deleted",
             description="List ONLY soft-deleted workflows (the trash bin).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_agent",
             description=("Fetch one agent's full spec by slug — includes "
                          "description, system_prompt, **use_cases**, model_slug, "
                          "tool_specs, etc. Use to deep-dive on an agent before "
                          "dispatching it."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="get_workflow",
             description=("Fetch one workflow's full spec (description, "
                          "**use_cases**, kind, graph). Use to deep-dive before "
                          "running."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="list_models",
             description="List configured LLM models (provider + model_id).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="list_tools",
             description=("List built-in / MCP / skill tools that agents can be "
                          "given via the `tool_specs` field."),
             inputSchema={"type": "object", "properties": {}}),

        # ----- create -----
        Tool(name="create_agent",
             description=("Create a brand-new agent.\n\n"
                          "Example:\n"
                          "```json\n"
                          "{\"name\":\"create_agent\",\"arguments\":{\n"
                          "  \"slug\":\"reviewer-strict\",\n"
                          "  \"name\":\"Strict Reviewer\",\n"
                          "  \"description\":\"Critiques code changes.\",\n"
                          "  \"system_prompt\":\"You are a strict code reviewer.\",\n"
                          "  \"model_slug\":\"claude-sonnet\",\n"
                          "  \"tool_specs\":[\"code.read_file\"]\n"
                          "}}\n"
                          "```"),
             inputSchema=_agent_spec_schema()),
        Tool(name="create_workflow",
             description=("Create a brand-new workflow.\n\n"
                          "See the server description for full examples. "
                          "`graph.max_hops` / `graph.max_tokens` set safety caps; "
                          "leaving them out uses platform defaults (50 hops, unlimited tokens)."),
             inputSchema=_workflow_spec_schema()),

        # ----- update -----
        Tool(name="update_agent",
             description=("Patch an agent. Only the fields you send are changed. "
                          "Use this to tweak prompt/model/tools without retyping the rest.\n\n"
                          "Example:\n"
                          "```json\n"
                          "{\"name\":\"update_agent\",\"arguments\":{\n"
                          "  \"slug\":\"reviewer-strict\",\n"
                          "  \"system_prompt\":\"You are an even stricter reviewer.\"\n"
                          "}}\n"
                          "```"),
             inputSchema=_agent_patch_schema()),
        Tool(name="update_workflow",
             description=("Patch a workflow. Sending a `graph` REPLACES the graph "
                          "wholesale — fetch via `get_workflow` first if you only "
                          "want to tweak one field of it."),
             inputSchema=_workflow_patch_schema()),

        # ----- delete / restore -----
        Tool(name="delete_agent",
             description=("Soft-delete an agent. The row keeps existing and can be "
                          "restored. Pass `hard:true` to permanently delete.\n\n"
                          "Example: `{\"name\":\"delete_agent\",\"arguments\":{\"slug\":\"reviewer-strict\"}}`"),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "hard": {"type": "boolean"}},
                          "required": ["slug"]}),
        Tool(name="delete_workflow",
             description=("Soft-delete a workflow. Pass `hard:true` for irreversible delete."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "hard": {"type": "boolean"}},
                          "required": ["slug"]}),
        Tool(name="restore_agent",
             description="Undo a soft-delete on an agent.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="restore_workflow",
             description="Undo a soft-delete on a workflow.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),

        # ----- execution -----
        Tool(name="run_agent_async",
             description=("Start an agent run in the background and return its run_id. "
                          "Poll with `run_status` / `run_events`, or block with `wait_run`.\n\n"
                          "**Call-me-back (default ON):** when this run finishes, YOUR session "
                          "is automatically re-invoked with a summary of its result, and that "
                          "reply is delivered down whatever channel started your own "
                          "conversation (Telegram, Watch, etc.) — no polling needed. Pass "
                          "`call_me_back:false` to opt out and get pure fire-and-forget "
                          "(the old behavior) instead.\n\n"
                          "**Redirect the callback (`call_me_back_on`):** by default the "
                          "callback wakes YOUR OWN session. Pass `call_me_back_on:<session_id>` "
                          "to wake a DIFFERENT session instead once this dispatched run "
                          "finishes — lets you chain a hand-off (\"run agent B, and when B is "
                          "done wake up session C\") in one call instead of B having to call C "
                          "itself. Ignored if `call_me_back:false` is also set.\n\n"
                          "**`target_slug` is REQUIRED.** Create a Target first with "
                          "`create_target` if none exists. Calls without `target_slug` "
                          "are rejected with a 400 error."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "input": {"type": "string"},
                                         "target_slug": {"type": "string",
                                                         "description": "Slug of the Target this run is delivering against. REQUIRED."},
                                         "target_id": {"type": ["string", "null"]},
                                         "session_id": {"type": ["string", "null"],
                                                        "description": "Resume a prior CLI session. Pass the session_id from a previous run's result to continue the conversation."},
                                         "notion_task_id": {"type": ["string", "null"],
                                                            "description": "Notion page ID of the Kanban card that originated this run. When set, the agent receives NOTION_TASK_ID env var and awserv sends a Telegram notification on completion."},
                                         "call_me_back": {"type": "boolean", "default": True,
                                                          "description": "Default true: when the dispatched run finishes, your own session gets woken up with its result and replies down your own channel automatically. Set false for pure fire-and-forget."},
                                         "call_me_back_on": {"type": ["string", "null"],
                                                             "description": "Optional session_id to redirect the callback to instead of your own session — e.g. a session_id returned by a prior run_agent_async call. That session's agent gets woken with this run's result instead of you."}},
                          "required": ["slug", "input", "target_slug"]}),
        Tool(name="run_workflow_async",
             description=("Start a workflow run in the background and return its run_id.\n\n"
                          "**Call-me-back (default ON):** same as `run_agent_async` — your "
                          "session is woken with the workflow's result when it finishes and "
                          "replies down your own channel, unless `call_me_back:false`.\n\n"
                          "**Redirect the callback (`call_me_back_on`):** same as "
                          "`run_agent_async` — pass a session_id to wake THAT session instead "
                          "of your own once this workflow finishes.\n\n"
                          "**`target_slug` is REQUIRED.** Create a Target first with "
                          "`create_target` if none exists. Calls without `target_slug` "
                          "are rejected with a 400 error."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "input": {"type": "string"},
                                         "target_slug": {"type": "string",
                                                         "description": "Slug of the Target this run is delivering against. REQUIRED."},
                                         "target_id": {"type": ["string", "null"]},
                                         "call_me_back": {"type": "boolean", "default": True,
                                                          "description": "Default true: when this workflow finishes, your own session gets woken up with its result and replies down your own channel automatically. Set false for pure fire-and-forget."},
                                         "call_me_back_on": {"type": ["string", "null"],
                                                             "description": "Optional session_id to redirect the callback to instead of your own session."}},
                          "required": ["slug", "input", "target_slug"]}),
        Tool(name="run_monitor_async",
             description=("Run a raw shell command in the background, isolated in its own "
                          "docker container, with NO LLM in the loop — and get woken back up "
                          "when it finishes. Use this instead of the harness's own built-in "
                          "`Monitor` tool for anything you need to watch a long-running shell "
                          "command for: that tool doesn't reliably wake this session back up in "
                          "this project. `run_monitor_async` rides the same proven wakeup rails "
                          "as `run_agent_async` instead.\n\n"
                          "**Call-me-back (default ON):** same semantics as `run_agent_async` — "
                          "when the command finishes, your session is automatically re-invoked "
                          "with the exit code, a short tail of output, and instructions for "
                          "fetching the full stdout/stderr (`get_run_artefact(run_id, "
                          "name=\"monitor_output\")`). Pass `call_me_back:false` for pure "
                          "fire-and-forget.\n\n"
                          "**Redirect the callback (`call_me_back_on`):** same as "
                          "`run_agent_async` — pass a session_id to wake a different session "
                          "instead of your own once the command finishes.\n\n"
                          "**`target_slug` is REQUIRED.** Create a Target first with "
                          "`create_target` if none exists."),
             inputSchema={"type": "object",
                          "properties": {"command": {"type": "string",
                                                      "description": "Shell command to execute (runs as `bash -lc \"<command>\"` inside an isolated container with the workspace mounted at /workspace)."},
                                         "target_slug": {"type": "string",
                                                         "description": "Slug of the Target this run is delivering against. REQUIRED."},
                                         "target_id": {"type": ["string", "null"]},
                                         "cwd": {"type": ["string", "null"],
                                                 "description": "Working directory, relative to the workspace root (e.g. 'repos/agents-platform'). Omit to run from the workspace root."},
                                         "timeout_seconds": {"type": "integer", "default": 3600,
                                                              "description": "Kill the command if it hasn't exited after this many seconds (max 86400 = 24h)."},
                                         "label": {"type": ["string", "null"],
                                                   "description": "Optional human-readable name for this monitor run, shown in the Runs list."},
                                         "notion_task_id": {"type": ["string", "null"]},
                                         "call_me_back": {"type": "boolean", "default": True,
                                                          "description": "Default true: when the command finishes, your own session gets woken up with exit_code + output instructions. Set false for pure fire-and-forget."},
                                         "call_me_back_on": {"type": ["string", "null"],
                                                             "description": "Optional session_id to redirect the callback to instead of your own session."}},
                          "required": ["command", "target_slug"]}),
        Tool(name="return_to_caller_agent",
             description=("Agentic-flow action: send `message` back to whoever called YOU "
                          "(via run_agent_async), resuming their exact session with full "
                          "context — not a fresh, memory-less dispatch of the same agent. "
                          "Use this when you were handed off a task and have a result, a "
                          "question, or anything else to report back to that specific caller.\n\n"
                          "`kind` is REQUIRED and structured — pick the one that matches what "
                          "you're actually sending, don't default to 'result' out of habit:\n"
                          "- `result`: you finished your part; this is the outcome/answer.\n"
                          "- `question`: you need the caller to decide something before you "
                          "can continue — they should expect to reply, not just receive.\n"
                          "- `blocker`: you got stuck (missing access, ambiguous ask, external "
                          "failure) and can't proceed without help.\n\n"
                          "No-op if the run that called you already has call_me_back=true — "
                          "in that case the caller is woken up automatically when your run "
                          "ends, and calling this too would double-resume it. Still returns "
                          "{ok:true, noop:true} in that case, not an error — safe to always "
                          "call as your \"report back\" action without checking first.\n\n"
                          "Fails with a clear reason if you have no caller (you're the root "
                          "of the chain, e.g. a Kanban `Ready`-dispatched run) — in that case "
                          "there's nothing to return to; move the Kanban card status instead."),
             inputSchema={"type": "object",
                          "properties": {"message": {"type": "string",
                                                     "description": "What to tell your caller — free text, the human-readable detail."},
                                        "kind": {"type": "string",
                                                 "enum": ["result", "question", "blocker"],
                                                 "description": "What you're sending back — result | question | blocker. Required."}},
                          "required": ["message", "kind"]}),
        Tool(name="ask_human",
             description=("Ask the human a question when you genuinely can't decide or don't "
                          "know how to proceed — sends it to the sysadmin Telegram bot as a "
                          "clickable link (a small page showing your question with a text box "
                          "to answer). Works whether or not this run carries a Kanban card — "
                          "no card required. This call itself just sends the question and "
                          "returns; you do NOT poll for the answer. When the human answers, "
                          "your session is automatically resumed with their answer as the next "
                          "prompt (same mechanism used for Agents Flow wakeups), so simply stop "
                          "here for this turn.\n\n"
                          "If this run also carries a Kanban card, ALSO call "
                          "`move_kanban_task(status='need_human', comment=...)` yourself — this "
                          "tool does not move Kanban cards, it only reaches the human directly."),
             inputSchema={"type": "object",
                          "properties": {"question": {"type": "string",
                                                      "description": "The question/decision needed, in plain language. Be specific — this is shown verbatim to the human."}},
                          "required": ["question"]}),
        Tool(name="mark_flow_done",
             description=("Agentic-flow action: declare the task you were dispatched to do "
                          "FINISHED — the third terminal action, alongside calling another "
                          "agent (run_agent_async) and reporting to your caller "
                          "(return_to_caller_agent). Use this when you completed the work "
                          "yourself and there's nothing left to hand off or report.\n\n"
                          "`outcome` is REQUIRED and structured — it decides the Kanban status, "
                          "not just informational text:\n"
                          "- `success`: fully done as asked → card moves to `done`.\n"
                          "- `partial`: you did meaningful work but it's incomplete/degraded → "
                          "card still moves to `done` (there's nothing more YOU can do), explain "
                          "the gap in `summary`.\n"
                          "- `failed`: the task did NOT conclude successfully → card moves to "
                          "`need_human` instead, with `summary` as the required explanation of "
                          "what went wrong (same rule as `need_human` elsewhere: problem, what "
                          "you tried, what's needed).\n\n"
                          "If this run is tied to a Kanban card (you have a NOTION_TASK_ID), "
                          "this moves that card for you — you don't need to call "
                          "move_kanban_task yourself. Without a card, marking the run is the "
                          "only effect.\n\n"
                          "QA accountability — pass `qa_run_id` (the Run.id of the QA agent run that "
                          "reviewed this work) or `qa_not_needed=true` (explicit \"no QA applies here\", "
                          "e.g. docs-only/trivial change) if you know which applies. Never both. If you "
                          "pass NEITHER, the backend auto-looks-up a recent succeeded QA-agent run "
                          "against this same card/target before giving up — useful in a multi-hop flow "
                          "where a different hop already ran QA and you don't know its run_id. Only "
                          "rejected (asking you to pick one explicitly) when no such run is found."),
             inputSchema={"type": "object",
                          "properties": {"summary": {"type": "string",
                                                     "description": "What was done (or what went wrong, if outcome=failed) — becomes the Kanban card comment if there's a card. Required when outcome='failed'."},
                                        "outcome": {"type": "string",
                                                    "enum": ["success", "partial", "failed"],
                                                    "description": "How the task concluded — success | partial | failed. Required."},
                                        "qa_run_id": {"type": "string",
                                                      "description": "Run.id of the QA agent run that reviewed this work. Mutually exclusive with qa_not_needed — exactly one is required."},
                                        "qa_not_needed": {"type": "boolean",
                                                          "description": "True if no QA pass applies to this task. Mutually exclusive with qa_run_id — exactly one is required."}},
                          "required": ["outcome"]}),
        Tool(name="mark_as_planned",
             description=("Agentic-flow action for PLANNING work — use this instead of "
                          "`mark_flow_done` when what you concluded is a PLAN/design/spec, not "
                          "a shippable implementation (e.g. Architect finishing a design, a "
                          "planning pass before building). `mark_flow_done(outcome='success')` "
                          "means 'the feature is done' — this means 'the plan now exists and is "
                          "ready for someone to build against', which is a different claim and "
                          "moves the card to a different column (`planned`, not `done`).\n\n"
                          "Counts as a valid way to end your flow turn (same as the 3 other "
                          "terminal actions) — no extra action needed alongside it.\n\n"
                          "If this run is tied to a Kanban card (NOTION_TASK_ID set), moves it to "
                          "`planned` with `summary` as the card comment. Without a card, marking "
                          "the run is the only effect — the plan lives in this run's own output, "
                          "which is where it belongs when there's nothing to persist it to.\n\n"
                          "No QA accountability required (there's no code yet to review)."),
             inputSchema={"type": "object",
                          "properties": {"summary": {"type": "string",
                                                     "description": "Summary of what was planned — becomes the Kanban card comment if there's a card."}},
                          "required": []}),
        Tool(name="register_callback",
             description=("Retroactively subscribe to a run's completion — for when you "
                          "dispatched `run_agent_async`/`run_workflow_async` with "
                          "`call_me_back:false` (or the run was started some other way) and "
                          "now want to be woken up when it finishes after all, instead of "
                          "polling `run_status`/`wait_run` yourself. Reuses the exact same "
                          "'call me back' delivery as `call_me_back:true` — your session (or "
                          "`session_id`, if given) is re-invoked with the run's result and "
                          "the reply goes down whatever channel started that conversation.\n\n"
                          "If `run_id` has already finished by the time you call this, the "
                          "wake-up fires immediately with its result — it does not silently "
                          "no-op.\n\n"
                          "`session_id` is optional — same as `call_me_back_on`: omit it to "
                          "wake your OWN session, or pass a session_id to wake a different one "
                          "instead."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string",
                                                    "description": "The already-dispatched run to watch. REQUIRED."},
                                        "session_id": {"type": ["string", "null"],
                                                       "description": "Optional session_id to wake instead of your own — same as call_me_back_on."}},
                          "required": ["run_id"]}),
        Tool(name="supervise",
             description=("Watch another session's whole run chain (that session plus every "
                          "descendant session/run it spawns) and get woken up on YOUR OWN "
                          "session exactly once when that chain goes idle (no running work "
                          "anywhere in it for 60s). Profile-agnostic mechanism — any agent can "
                          "supervise any other session, not just a 'Manager' persona; this tool "
                          "only reports STATE (who ran, how long, why it stopped) — deciding "
                          "whether that's good or bad vs your goal is on you, the caller.\n\n"
                          "Idempotent: calling this again for the same target_session_id "
                          "returns the SAME supervision_id (existing:true) instead of arming a "
                          "duplicate watcher.\n\n"
                          "Two modes:\n"
                          "- forever=false (default, one-shot): after the first wakeup, this "
                          "  supervision stops watching the old chain and instead watches only "
                          "  YOUR session — if you dispatch something new within 60s it resumes "
                          "  full tracking, otherwise it quietly gives up (status='done').\n"
                          "- forever=true: keeps delivering one wakeup per running->idle "
                          "  transition, indefinitely, until you call stop_supervisor.\n\n"
                          "Rejected if target_session_id doesn't exist, or if it's your own "
                          "session (guaranteed self-trigger loop).\n\n"
                          "Wakeup dedup: repeated wakeups for the SAME underlying cause "
                          "(same card, same coder run finishing again) within a short debounce "
                          "window are suppressed instead of re-firing — see supervisor_status's "
                          "wakeup_history/suppressed_count. Default window 90s; override with "
                          "debounce_s."),
             inputSchema={"type": "object",
                          "properties": {"session_id": {"type": "string",
                                                        "description": "The session_id to supervise. REQUIRED."},
                                        "forever": {"type": "boolean", "default": False,
                                                   "description": "Keep re-arming after each wakeup instead of stopping after one retrigger-or-give-up cycle. Default false."},
                                        "debounce_s": {"type": ["integer", "null"],
                                                      "description": "Override the default 90s wakeup-dedup window for this supervision."}},
                          "required": ["session_id"]}),
        Tool(name="stop_supervisor",
             description="Cancel an active supervision armed via `supervise` — no more wakeups from it.",
             inputSchema={"type": "object",
                          "properties": {"supervision_id": {"type": "string",
                                                            "description": "The supervision_id returned by `supervise`. REQUIRED."}},
                          "required": ["supervision_id"]}),
        Tool(name="supervisor_status",
             description=("Inspect your active supervisions. Without `supervision_id`: lists "
                          "every supervision YOU armed (id, target, status, forever, "
                          "wakeup_count). With `supervision_id`: full detail — discovered "
                          "sessions/runs, edge_state, idle_since, stop_reason, wakeup history."),
             inputSchema={"type": "object",
                          "properties": {"supervision_id": {"type": ["string", "null"],
                                                            "description": "Optional — omit to list all of your own supervisions instead."}},
                          "required": []}),
        Tool(name="run_status",
             description=("Return the current Run row: status, output, error, tokens, "
                          "and (for workflows) `limit_reached` if a budget cap stopped it.\n\n"
                          "Pass `summary:true` to truncate huge `input` fields (~200 chars) "
                          "so you don't echo your own prompt back in every poll."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "summary": {"type": "boolean",
                                                     "description": "Truncate huge `input` fields to ~200 chars."}},
                          "required": ["run_id"]}),
        Tool(name="run_events",
             description=("Return the ordered event stream for a run (node_start, "
                          "node_end, tool_call, tool_result, llm_token, error, done). "
                          "Useful for observing how a workflow progressed.\n\n"
                          "Use `after_ts` (ISO8601) as a cursor to tail just new events. "
                          "Use `kinds` (comma list) to filter by event kind. "
                          "E.g. `{\"run_id\":\"…\",\"kinds\":\"node_start,error,done\"}`."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "after_ts": {"type": "string",
                                                      "description": "Only events with ts > this ISO8601 timestamp."},
                                         "kinds": {"type": "string",
                                                   "description": "Comma-separated kinds to keep (omit for all)."},
                                         "limit": {"type": "integer"}},
                          "required": ["run_id"]}),
        Tool(name="wait_run",
             description=("BLOCK until the run reaches a terminal status "
                          "(success|error|cancelled) or until `timeout_s` elapses, "
                          "then return the full RunOut snapshot. **Eliminates the "
                          "polling pattern** — caller doesn't need to call "
                          "`run_status` in a loop. On timeout, returns the row in its "
                          "current state without raising; check `status` to detect.\n\n"
                          "Optional `max_cost_usd`: if the rolled-up cost (run + "
                          "descendants) exceeds this mid-wait, the run is cancelled "
                          "(cascading) and the snapshot returned with status='cancelled' "
                          "and reason='cost_cap'."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "timeout_s": {"type": "integer",
                                                       "description": "Hard cap on the wait (1..3600). Default 300."},
                                         "poll_interval_s": {"type": "number",
                                                             "description": "Server-side internal poll cadence (0.25..30). Default 2."},
                                         "max_cost_usd": {"type": "number",
                                                          "description": "Cancel if rolled-up cost exceeds this USD value mid-wait. Default: no cap."},
                                         "summary": {"type": "boolean"}},
                          "required": ["run_id"]}),
        Tool(name="peek_run_output",
             description=("MID-FLIGHT snapshot of a running run. Returns last N events, "
                          "accumulated streamed text (if any), tokens/cost so far, and "
                          "current output buffer. Lets you catch a misbehaving agent "
                          "BEFORE waiting for terminal. NO polling required."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "tail_events": {"type": "integer",
                                                         "description": "How many trailing events to return (1..200). Default 20."}},
                          "required": ["run_id"]}),
        Tool(name="run_agents_parallel",
             description=("FAN OUT N agent runs in parallel under a synthetic parent "
                          "run. **No row is written to the `workflows` table** — the "
                          "parent is ephemeral (kind='workflow', target_slug='_ephemeral_parallel_'). "
                          "Returns parent_run_id + child_run_ids so you can `wait_run` "
                          "on the parent or roll up costs via `run_tree`.\n\n"
                          "Use this instead of N separate `run_agent_async` calls when "
                          "you want shared lineage + cost rollup. Max 20 children per "
                          "dispatch.\n\n"
                          "Example:\n"
                          "```json\n"
                          "{\"name\":\"run_agents_parallel\",\"arguments\":{\n"
                          "  \"agents\":[\n"
                          "    {\"slug\":\"explorer\",\"input\":\"locate the auth code\"},\n"
                          "    {\"slug\":\"researcher\",\"input\":\"web research session lib\"}\n"
                          "  ],\n"
                          "  \"target_slug\":\"us1924311-acsb-alerts\"\n"
                          "}}\n"
                          "```\n\n"
                          "**`target_slug` is REQUIRED.** Calls without it are rejected with a 400 error."),
             inputSchema={"type": "object",
                          "properties": {
                              "agents": {"type": "array", "minItems": 1, "maxItems": 20,
                                         "items": {"type": "object", "properties": {
                                             "slug": {"type": "string"},
                                             "input": {"type": "string"},
                                             "node_id": {"type": "string"},
                                         }, "required": ["slug"]}},
                              "target_id": {"type": ["string", "null"]},
                              "target_slug": {"type": "string",
                                              "description": "Slug of the Target this fan-out is delivering against. REQUIRED."},
                              "max_hops": {"type": ["integer", "null"]},
                              "max_tokens": {"type": ["integer", "null"]},
                          },
                          "required": ["agents", "target_slug"]}),
        Tool(name="run_tree",
             description=("Recursive run tree (root + descendants) with rolled-up "
                          "totals — tokens, costs, hops/tokens budget."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"}},
                          "required": ["run_id"]}),
        Tool(name="cancel_run",
             description=("Cancel a running run. For workflows the cancel cascades "
                          "through all descendants."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"}},
                          "required": ["run_id"]}),
        Tool(name="cancel_all_runs",
             description="Cancel every currently-running run on the platform.",
             inputSchema={"type": "object", "properties": {}}),

        # ----- self-service session hygiene -----
        Tool(name="clear_session",
             description=("Queue a hard reset of the CURRENT conversation's CLI session. "
                          "Does NOT act immediately (a running session can't be reset "
                          "mid-turn) — it's applied automatically right before your NEXT "
                          "turn in this same session, before that turn's message is "
                          "processed, then removed from the queue. The next turn effectively "
                          "starts a brand-new session (no memory of this one). Call this "
                          "when the user asks to 'clear the session' / 'limpa a sessão' / "
                          "'reseta a conversa'. You need your own session_id — get it with "
                          "`echo $AW_SESSION_ID` (Bash tool) if you don't already have it."),
             inputSchema={"type": "object",
                          "properties": {"session_id": {"type": "string",
                              "description": "This session's claude CLI session_id (from $AW_SESSION_ID)."}},
                          "required": ["session_id"]}),
        Tool(name="compact_session",
             description=("Queue a /compact (context summarization) for the CURRENT "
                          "conversation's CLI session. Applied automatically right before "
                          "your NEXT turn in this same session, before that turn's message "
                          "is processed, then removed from the queue — the conversation "
                          "keeps going, just with a shorter context. Call this when the user "
                          "asks to 'compact the session' / 'compacta a sessão'. You need "
                          "your own session_id — get it with `echo $AW_SESSION_ID` (Bash "
                          "tool) if you don't already have it."),
             inputSchema={"type": "object",
                          "properties": {"session_id": {"type": "string",
                              "description": "This session's claude CLI session_id (from $AW_SESSION_ID)."}},
                          "required": ["session_id"]}),
        Tool(name="rename_session",
             description=("Rename the CURRENT conversation's CLI session — same effect as "
                          "Telegram's `/rename`, just callable as a tool (e.g. from the "
                          "Apple Watch app, which has no /rename command). Unlike "
                          "clear_session/compact_session this applies IMMEDIATELY (it's just "
                          "a label on the CliSession row, not a mid-turn CLI action) and the "
                          "name persists everywhere that session is shown — Telegram, watch, "
                          "list_sessions/list_bots, etc. Call this when the user asks to "
                          "'rename the session' / 'renomeia a sessão' / 'dá um nome pra essa "
                          "conversa'. You need your own session_id — get it with "
                          "`echo $AW_SESSION_ID` (Bash tool) if you don't already have it."),
             inputSchema={"type": "object",
                          "properties": {"session_id": {"type": "string",
                              "description": "This session's claude CLI session_id (from $AW_SESSION_ID)."},
                              "name": {"type": "string",
                              "description": "New display name for the session. Empty string clears it back to unnamed."}},
                          "required": ["session_id", "name"]}),

        # ----- legacy alias -----
        Tool(name="get_run",
             description="Alias for run_status — fetches a Run row by id.",
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"}},
                          "required": ["run_id"]}),

        # ----- global run listing / deep-dive -----
        Tool(name="list_runs",
             description=("GLOBAL run listing across ALL targets, ordered by recency "
                          "(most recent first). Unlike `list_target_runs` this is not "
                          "scoped to one Target. Each row: run_id, target_slug, "
                          "agent/source_slug, status, started_at, ended_at, a truncated "
                          "`input` preview, cost_usd, tokens_in, tokens_out.\n\n"
                          "No hard cap on `limit` — pass 100-200+ for a wide recency "
                          "window."),
             inputSchema={"type": "object",
                          "properties": {"limit": {"type": "integer",
                                                    "description": "Max rows to return. Default 20."},
                                         "status": {"type": "string",
                                                    "description": "Filter by status: pending|running|success|error|cancelled."}}}),
        Tool(name="get_run_detail",
             description=("FULL record for one Run: untruncated input/output, status, "
                          "timestamps, target_slug, agent/source_slug, cost, tokens, "
                          "error, session_id — PLUS the ordered event trace (tool_call/"
                          "tool_result/node_start/node_end/…). Combines `get_run` + "
                          "`run_events` in a single call. Use plain `get_run`/"
                          "`run_status` instead if you don't need the event trace."),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "events_limit": {"type": "integer",
                                                           "description": "Cap on returned events. Default 500."}},
                          "required": ["run_id"]}),
        Tool(name="list_bots",
             description=("Per-bot (per-agent) snapshot: for every agent/source_slug seen "
                          "in recent runs, its currently active session_id (the session_id "
                          "of its most recent kind=agent run), that session's user-renamed "
                          "name if one was set (from `cli_sessions.name` — see `list_sessions`"
                          "-equivalent lookup), and every run tied to that active session "
                          "with its status. Shape: [{bot, session_id, renamed_name, "
                          "runs: [{run_id, status}, ...]}, ...].\n\n"
                          "Scans the `scan_limit` most recent agent runs to build the bot "
                          "list — raise it if a bot you expect is missing."),
             inputSchema={"type": "object",
                          "properties": {"scan_limit": {"type": "integer",
                              "description": "How many recent agent runs to scan when "
                                             "grouping by bot. Default 500."}}}),
        Tool(name="list_sessions",
             description=("List CLI sessions (claude --resume sessions), most recently "
                          "updated first — unlike `list_bots` (which only shows each "
                          "agent's SINGLE currently-active session), this returns the full "
                          "session HISTORY for an agent. Each row: session_id, name "
                          "(user-renamed label, if any), description, run_count, "
                          "last_run_at, last_status.\n\n"
                          "Pass `source_slug` to scope to one agent/workflow (e.g. "
                          "'crispal-dev-sonnet') — only sessions with at least one run "
                          "from that slug are returned. Pass `q` to search session_id/"
                          "name/description. Omit both to list across every agent."),
             inputSchema={"type": "object",
                          "properties": {
                              "source_slug": {"type": "string",
                                  "description": "Only sessions with at least one run from "
                                                 "this agent/workflow slug (e.g. 'crispal-dev-sonnet')."},
                              "q": {"type": "string",
                                  "description": "Free-text search over session_id/name/description."},
                              "limit": {"type": "integer",
                                  "description": "Max rows to return. Default 100, max 500."},
                          }}),

        # ----- Targets (overall delivery goals) -----
        Tool(name="list_callers",
             description=("List external caller identities (e.g. Roblox players) that have "
                          "hit /v1/chat/completions with X-Caller-Meta-* headers. Each caller "
                          "is keyed by (source, external_id), e.g. source='roblox', "
                          "external_id='<UserId>'."),
             inputSchema={"type": "object", "properties": {
                 "source": {"type": "string", "description": "Filter by source, e.g. 'roblox'."},
                 "meta_filter": {"type": "string",
                                 "description": "JSON object matched via JSONB containment against "
                                                "meta_info, e.g. '{\"membershipType\": \"Premium\"}'."},
             }}),
        Tool(name="list_caller_messages",
             description=("Fetch one caller's full message history (both what they sent and "
                          "what the agent replied), oldest first. Use `list_callers` first to "
                          "find the (source, external_id) pair."),
             inputSchema={"type": "object", "properties": {
                 "source": {"type": "string"},
                 "external_id": {"type": "string"},
                 "limit": {"type": "integer", "description": "Max messages to return (default 50)."},
             }, "required": ["source", "external_id"]}),
        Tool(name="list_targets",
             description=("List Targets — first-class umbrella goals that group runs. "
                          "Each Target represents the WHY of an orchestration (e.g. "
                          "'deliver US1924311'). Use this to find existing Targets, "
                          "or read its retro view via `target_summary`."),
             inputSchema={"type": "object", "properties": {
                 "include_deleted": {"type": "boolean"},
                 "status": {"type": "string",
                            "description": "Filter by status: active|completed|cancelled|abandoned."},
                 "q": {"type": "string", "description": "Search slug/name/description/source_ref."},
                 "limit": {"type": "integer"},
             }}),
        Tool(name="get_target",
             description="Fetch one Target's full spec by slug.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="create_target",
             description=("Create a new Target — the umbrella goal a tree of runs "
                          "is delivering against. Set this at the START of an "
                          "orchestration; pass `target_slug` to subsequent "
                          "`run_agent_async` / `run_agents_parallel` calls to link "
                          "every run back.\n\n"
                          "Example (Rally-derived):\n"
                          "```json\n"
                          "{\"name\":\"create_target\",\"arguments\":{\n"
                          "  \"slug\":\"us1924311-acsb-alerts\",\n"
                          "  \"name\":\"US1924311 — ACSB alerts for dr-external-api\",\n"
                          "  \"description\":\"Bootstrap ACSB gold-signal monitoring + open PR.\",\n"
                          "  \"source_kind\":\"rally_story\",\n"
                          "  \"source_ref\":\"US1924311\",\n"
                          "  \"budget_usd\":20,\n"
                          "  \"budget_tokens\":800000\n"
                          "}}\n"
                          "```"),
             inputSchema=_target_spec_schema()),
        Tool(name="update_target",
             description=("Patch a Target — set status to 'completed'/'cancelled' to "
                          "stamp ended_at, attach plan_canvas_id / report_canvas_id, "
                          "or update budgets."),
             inputSchema=_target_patch_schema()),
        Tool(name="delete_target",
             description=("Soft-delete by default (recoverable). Pass `hard:true` for "
                          "irreversible delete, which also nulls runs.target_id "
                          "pointing at this Target."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "hard": {"type": "boolean"}},
                          "required": ["slug"]}),
        Tool(name="restore_target",
             description="Undo a soft-delete on a Target.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="target_summary",
             description=("Retro view — rolled-up stats across every Run linked to a "
                          "Target: total runs (by status), tokens in/out, cost $, "
                          "agents used (with counts), models used, wall time, and "
                          "percent of budget consumed. Use this for end-of-delivery "
                          "reports + post-mortems."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"}},
                          "required": ["slug"]}),
        Tool(name="list_target_runs",
             description=("List every Run linked to a Target, chronologically. "
                          "Lightweight (no events). For deep-dive on any single run "
                          "use `run_tree`."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "limit": {"type": "integer"}},
                          "required": ["slug"]}),
        Tool(name="link_run_to_target",
             description=("LINK an existing run (and its whole subtree by default) "
                          "to a Target. Use this for retroactive linkage when a run "
                          "was dispatched without a target_slug, or to fix mis-tagged "
                          "runs. Idempotent."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "run_id": {"type": "string"},
                                         "include_descendants": {"type": "boolean",
                                                                 "description": "Also link every descendant in the run tree. Default true."}},
                          "required": ["slug", "run_id"]}),
        Tool(name="unlink_run_from_target",
             description="Reverse of link_run_to_target — set runs.target_id = NULL.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "run_id": {"type": "string"},
                                         "include_descendants": {"type": "boolean"}},
                          "required": ["slug", "run_id"]}),
        Tool(name="attach_pr_to_target",
             description=("Attach a PR URL (with optional title/status/ci_status) to "
                          "a Target. Idempotent on URL — re-attaching the same URL "
                          "updates the metadata in place. Use this so the retro view "
                          "shows the PRs that delivered the Target."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "url": {"type": "string"},
                                         "title": {"type": "string"},
                                         "status": {"type": "string", "enum": ["open", "merged", "closed"]},
                                         "ci_status": {"type": "string", "enum": ["passing", "failing", "pending"]},
                                         "notes": {"type": "string"}},
                          "required": ["slug", "url"]}),
        Tool(name="detach_pr_from_target",
             description="Remove a PR URL from a Target.",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "url": {"type": "string"}},
                          "required": ["slug", "url"]}),

        # ----- Target Lessons (the retro / continuous-learning store) -----
        Tool(name="search_lessons",
             description=("**Cross-target search** over the platform's lessons-learned store. "
                          "The `retro` agent calls this BEFORE creating new lessons, to find "
                          "existing ones it should UPDATE rather than duplicate. Future "
                          "deliveries call this at Phase 1.5 to surface relevant prior "
                          "lessons matching the task's tags.\n\n"
                          "Pass `tags` (comma-separated) to filter by applicable_tags overlap, "
                          "`category` to filter by lesson kind, or `q` for full-text search "
                          "over title + content."),
             inputSchema={"type": "object",
                          "properties": {"tags": {"type": "string",
                                                  "description": "Comma-separated tag list. e.g. 'cat-2,acsb,cookiecutter'"},
                                         "category": {"type": "string",
                                                      "description": "time-saver|pitfall|tooling-gap|pattern-that-worked|prompt-fix|cost-trap|scope-creep"},
                                         "q": {"type": "string"},
                                         "include_superseded": {"type": "boolean"},
                                         "limit": {"type": "integer"}}}),
        Tool(name="list_target_lessons",
             description=("List all lessons for one Target (scoped). Use this when running "
                          "a retro on a Target — see what's already been recorded."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "include_superseded": {"type": "boolean"}},
                          "required": ["slug"]}),
        Tool(name="create_target_lesson",
             description=("Record a new lesson against a Target. Use this AFTER "
                          "`search_lessons` confirms no existing lesson covers this insight.\n\n"
                          "`category`: time-saver | pitfall | tooling-gap | pattern-that-worked | "
                          "prompt-fix | cost-trap | scope-creep (or your own — these are conventions).\n"
                          "`evidence_run_ids`: list of run ids that motivated this lesson.\n"
                          "`applicable_tags`: tags future searches will match on (e.g. "
                          "`['cat-2','acsb','cookiecutter']`)."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "category": {"type": "string"},
                                         "title": {"type": "string"},
                                         "content": {"type": "string"},
                                         "evidence_run_ids": {"type": "array", "items": {"type": "string"}},
                                         "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                                         "applicable_tags": {"type": "array", "items": {"type": "string"}},
                                         "source": {"type": "string"},
                                         "created_in_run_id": {"type": "string", "description": "FK to the run that authored this lesson — typically the retro agent's own run_id. Required for the 'lesson → originating agent' UI navigation to work deterministically."},
                                         "status": {"type": "string", "enum": ["pending_review", "active", "archived"], "description": "Lesson lifecycle status. Default 'active'. Set 'pending_review' for auto-drafted low-score lessons that need human approval."}},
                          "required": ["slug", "category", "title"]}),
        Tool(name="update_target_lesson",
             description=("PATCH an existing lesson — used by the `retro` agent to extend a "
                          "prior lesson with new evidence (append to `evidence_run_ids`), refine "
                          "the body, or raise `confidence` as more deliveries confirm the pattern. "
                          "Pass `superseded_by` to mark a lesson replaced by a newer one."),
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "lesson_id": {"type": "string"},
                                         "category": {"type": "string"},
                                         "title": {"type": "string"},
                                         "content": {"type": "string"},
                                         "evidence_run_ids": {"type": "array", "items": {"type": "string"}},
                                         "confidence": {"type": "string"},
                                         "applicable_tags": {"type": "array", "items": {"type": "string"}},
                                         "superseded_by": {"type": "string"},
                                         "created_in_run_id": {"type": "string", "description": "FK to the run that authored this lesson — typically the retro agent's own run_id. Required for the 'lesson → originating agent' UI navigation to work deterministically."},
                                         "status": {"type": "string", "enum": ["pending_review", "active", "archived"], "description": "Lesson lifecycle status. Default 'active'. Set 'pending_review' for auto-drafted low-score lessons that need human approval."}},
                          "required": ["slug", "lesson_id"]}),
        Tool(name="delete_target_lesson",
             description="Soft-delete a lesson (or `hard:true` for irreversible).",
             inputSchema={"type": "object",
                          "properties": {"slug": {"type": "string"},
                                         "lesson_id": {"type": "string"},
                                         "hard": {"type": "boolean"}},
                          "required": ["slug", "lesson_id"]}),

        # ----- Lesson Applications (the tracking layer for continuous improvement) -----
        Tool(name="record_lesson_application",
             description=("Record that a lesson was surfaced / applied / rejected / "
                          "ignored for a specific Target (optionally tied to a run). "
                          "**The conductor MUST call this at Phase 1.5** with outcome="
                          "`shown_to_pm` for every lesson it passes to project-manager. "
                          "The retro agent calls it later with outcome=`applied` | `prevented` | "
                          "`ignored` | `partial` based on what it observes.\n\n"
                          "Outcomes: retrieved | shown_to_pm | applied | rejected | "
                          "prevented | ignored | partial"),
             inputSchema={"type": "object",
                          "properties": {"lesson_id": {"type": "string"},
                                         "target_id": {"type": "string",
                                                       "description": "Optional — defaults to lesson's home Target."},
                                         "applied_in_run_id": {"type": "string",
                                                               "description": "Optional — the specific run that applied or ignored the lesson."},
                                         "outcome": {"type": "string",
                                                     "enum": ["retrieved", "shown_to_pm", "applied", "rejected", "prevented", "ignored", "partial"]},
                                         "notes": {"type": "string"}},
                          "required": ["lesson_id", "outcome"]}),
        Tool(name="list_lesson_applications",
             description=("List application records. Filter by lesson_id, target_id, "
                          "or outcome. Use this to find lessons that have been "
                          "consistently IGNORED (biggest improvement opportunity)."),
             inputSchema={"type": "object",
                          "properties": {"lesson_id": {"type": "string"},
                                         "target_id": {"type": "string"},
                                         "outcome": {"type": "string"},
                                         "limit": {"type": "integer"}}}),
        Tool(name="lesson_effectiveness",
             description=("Per-lesson stats: how often was it applied vs ignored? "
                          "`effectiveness_rate` = (applied+prevented) / total. "
                          "`propagation_gap_rate` = ignored / shown_to_pm — high gap "
                          "means the lesson is well-known but still missed."),
             inputSchema={"type": "object",
                          "properties": {"lesson_id": {"type": "string"}},
                          "required": ["lesson_id"]}),
        Tool(name="lesson_forecast",
             description=("**PRE-FLIGHT FORECAST** for an upcoming task. Given tags + "
                          "category, returns:\n"
                          "  - matched_lessons: top-relevant lessons (by tag overlap)\n"
                          "  - similar_targets: prior Targets sharing tags\n"
                          "  - predicted_cost_usd / wall_seconds: p10/p50/p90 from priors\n"
                          "  - advisories: high-confidence pitfalls + proven patterns to apply\n\n"
                          "**Call this BEFORE Phase 1.5** to see what the platform knows about "
                          "similar tasks. The conductor uses it to set realistic budgets and "
                          "pick the right model tiers."),
             inputSchema={"type": "object",
                          "properties": {"tags": {"type": "array", "items": {"type": "string"}},
                                         "category": {"type": "string"},
                                         "description": {"type": "string"}}}),
        Tool(name="lessons_metrics",
             description=("Cross-target 'are we improving?' dashboard. Returns:\n"
                          "  - total_lessons by category / confidence\n"
                          "  - application outcomes breakdown\n"
                          "  - avg_effectiveness_rate · avg_propagation_gap_rate\n"
                          "  - cost_trend · wall_trend across Targets (chronological)\n"
                          "  - top_applied_lessons (most valuable) · top_ignored_lessons (biggest gaps)"),
             inputSchema={"type": "object", "properties": {}}),

        # ----- Retro scores (Wave-6 A3) -----
        Tool(name="list_retro_scores",
             description=("List quality scores for a run, broken down by dimension "
                          "(cost, wall, mistakes, lessons_applied, plan_adherence, "
                          "scope_discipline, accuracy, output_quality, recovery, overall). "
                          "Each score has a source (auto|retro_agent|human), rationale, "
                          "and evidence_json. By default only the active (non-superseded) "
                          "row per dimension is returned; pass include_superseded=true to "
                          "see the full override history."),
             inputSchema={"type": "object",
                          "properties": {
                              "run_id": {"type": "string", "description": "ID of the run to score."},
                              "include_superseded": {"type": "boolean",
                                                     "description": "If true, include rows superseded by human overrides."},
                          },
                          "required": ["run_id"]}),
        Tool(name="override_retro_score",
             description=("Write a human quality score for one dimension of a run. "
                          "Creates a new row with source='human', marks the previous "
                          "active row as superseded, then recomputes the overall score "
                          "and Run.retro_score_summary.\n\n"
                          "Valid dimensions: accuracy, output_quality, lessons_applied, "
                          "recovery, plan_adherence, cost, wall, mistakes, scope_discipline.\n"
                          "Score must be an integer 1–10 (10 = best)."),
             inputSchema={"type": "object",
                          "properties": {
                              "run_id": {"type": "string"},
                              "dimension": {"type": "string",
                                            "description": "accuracy|output_quality|lessons_applied|recovery|plan_adherence|cost|wall|mistakes|scope_discipline"},
                              "score": {"type": "integer", "minimum": 1, "maximum": 10},
                              "rationale": {"type": "string",
                                            "description": "Human-readable explanation for this score."},
                              "evidence_json": {"type": "object",
                                                "description": "Arbitrary structured evidence supporting the score."},
                          },
                          "required": ["run_id", "dimension", "score"]}),
        Tool(name="recompute_retro_score",
             description=("Re-run the auto-scorer (score_run_terminal) on a run that has "
                          "already completed. Useful for back-filling retro scores on older "
                          "runs, or refreshing scores after event data changes. "
                          "Returns {computed: bool, summary: {...}}. "
                          "The run must be in a terminal status (success|error|cancelled)."),
             inputSchema={"type": "object",
                          "properties": {
                              "run_id": {"type": "string"},
                          },
                          "required": ["run_id"]}),
        Tool(name="get_retro_score_weights",
             description=("Return the current dimension→weight map used to compute the "
                          "weighted-mean overall retro score. The 9 dimensions are: "
                          "accuracy, output_quality, lessons_applied, recovery, plan_adherence, "
                          "cost, wall, mistakes, scope_discipline. Weights sum to ~1.0. "
                          "Returns {weights: {dim: float}, updated_at}."),
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="set_retro_score_weights",
             description=("Update the dimension→weight map for overall retro-score computation. "
                          "All provided weights must be 0–1 and sum to ~1.0 (±0.01 tolerance). "
                          "Missing dimensions default to 0. 'overall' is computed and cannot "
                          "be set directly. Returns the updated {weights, updated_at}.\n\n"
                          "Example: {\"accuracy\": 0.30, \"output_quality\": 0.20, "
                          "\"lessons_applied\": 0.15, \"recovery\": 0.10, "
                          "\"plan_adherence\": 0.10, \"cost\": 0.05, \"wall\": 0.04, "
                          "\"mistakes\": 0.03, \"scope_discipline\": 0.03}"),
             inputSchema={"type": "object",
                          "properties": {
                              "weights": {"type": "object",
                                          "description": "Map of dimension → weight (float 0–1). Must sum to ~1.0.",
                                          "additionalProperties": {"type": "number"}},
                          },
                          "required": ["weights"]}),
        Tool(name="list_pending_lessons",
             description=("List lessons in 'pending_review' status — written by the retro "
                          "agent but not yet promoted to 'active'. Use this to review and "
                          "approve or archive AI-generated lessons before they propagate to "
                          "future deliveries. Supports pagination via limit/offset."),
             inputSchema={"type": "object",
                          "properties": {
                              "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                        "description": "Max lessons to return (default 50)."},
                              "offset": {"type": "integer", "minimum": 0,
                                         "description": "Pagination offset (default 0)."},
                          }}),
        Tool(name="approve_pending_lesson",
             description=("Promote a lesson from 'pending_review' to 'active' status. "
                          "Once active, the lesson is surfaced by search_lessons and "
                          "lesson_forecast for future deliveries. "
                          "Returns the updated LessonOut."),
             inputSchema={"type": "object",
                          "properties": {
                              "lesson_id": {"type": "string"},
                          },
                          "required": ["lesson_id"]}),
        Tool(name="archive_lesson",
             description=("Set a lesson's status to 'archived'. Archived lessons are "
                          "excluded from search_lessons and lesson_forecast results. "
                          "Use this to retire outdated or incorrect lessons without hard-deleting them. "
                          "Returns the updated LessonOut."),
             inputSchema={"type": "object",
                          "properties": {
                              "lesson_id": {"type": "string"},
                          },
                          "required": ["lesson_id"]}),

        # ----- Lesson consolidation (Wave-6 L2) -----
        Tool(name="consolidate_lessons",
             description=("Merge N existing lessons into ONE consolidated lesson. "
                          "The source lessons are archived (status='archived') with "
                          "superseded_by pointing to the new lesson. The new lesson "
                          "inherits the union of evidence_run_ids and applicable_tags.\n\n"
                          "Provide title and content (the merged body). category, "
                          "applicable_tags, and confidence default to majority/union/max "
                          "of the sources if omitted. target_id defaults to the first "
                          "source lesson's target.\n\n"
                          "Rejects if any source lesson is already archived."),
             inputSchema={"type": "object",
                          "properties": {
                              "lesson_ids": {"type": "array", "items": {"type": "string"},
                                             "description": "IDs of lessons to merge (min 2)."},
                              "title": {"type": "string", "description": "Title for the consolidated lesson."},
                              "content": {"type": "string", "description": "Merged markdown body."},
                              "category": {"type": "string",
                                           "description": "pitfall|time-saver|tooling-gap|pattern-that-worked|prompt-fix|cost-trap|scope-creep. Defaults to majority category of sources."},
                              "applicable_tags": {"type": "array", "items": {"type": "string"},
                                                  "description": "Tags for the new lesson. Defaults to union of source tags."},
                              "confidence": {"type": "string", "enum": ["low", "medium", "high"],
                                             "description": "Defaults to max confidence of sources."},
                              "target_id": {"type": "string",
                                            "description": "Target to attach to. Defaults to first source's target_id."},
                          },
                          "required": ["lesson_ids", "title", "content"]}),
        Tool(name="suggest_lesson_consolidation",
             description=("Find clusters of active lessons that are candidates for "
                          "consolidation, using deterministic tag + title overlap scoring "
                          "(no LLM). Two lessons join the same cluster if "
                          "(jaccard_tags ≥ 0.5 AND title_overlap ≥ 0.3) OR jaccard_tags ≥ 0.8.\n\n"
                          "Returns clusters sorted by confidence DESC. Each cluster "
                          "includes lesson_ids, reason (shared tags + title tokens), "
                          "confidence score, common_tags, and common_category.\n\n"
                          "Typical workflow: call this first, pick a cluster, optionally "
                          "call draft_consolidated_lesson for an AI-drafted merge, then "
                          "call consolidate_lessons to finalize."),
             inputSchema={"type": "object",
                          "properties": {
                              "min_overlap": {"type": "integer", "minimum": 1, "default": 1,
                                              "description": "Min number of common tags required to report a cluster."},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20,
                                        "description": "Max clusters to return."},
                              "min_cluster_size": {"type": "integer", "minimum": 2, "default": 2,
                                                   "description": "Min number of lessons in a reportable cluster."},
                          }}),
        Tool(name="draft_consolidated_lesson",
             description=("Kick off a planner agent run to draft a merged title + content "
                          "from N source lessons. Returns {run_id, status: 'running'} — "
                          "poll with run_status until done, then read output.final.\n\n"
                          "The planner output follows a strict template:\n"
                          "  TITLE: ...\n"
                          "  CATEGORY: ...\n"
                          "  TAGS: ...\n"
                          "  BODY:\\n<markdown>\n\n"
                          "Parse it and pass to consolidate_lessons for final write. "
                          "This is the ONLY LLM call in the consolidation flow — opt-in, "
                          "on explicit user request."),
             inputSchema={"type": "object",
                          "properties": {
                              "lesson_ids": {"type": "array", "items": {"type": "string"},
                                             "description": "IDs of lessons to draft a merge for (min 2)."},
                          },
                          "required": ["lesson_ids"]}),

        # ----- Run artefacts (structured outputs attached to a run) -----
        Tool(name="add_run_artefact",
             description=("Attach a named artefact (file blob) to a run. Use this "
                          "instead of cramming structured outputs (NRQL tables, "
                          "terraform plans, diffs, JSON configs) into `output.final` "
                          "as one string. Replaces by name if it already exists.\n\n"
                          "Example:\n"
                          "```json\n"
                          "{\"name\":\"add_run_artefact\",\"arguments\":{\n"
                          "  \"run_id\":\"abc123\",\n"
                          "  \"name\":\"nrql-baselines.md\",\n"
                          "  \"mime\":\"text/markdown\",\n"
                          "  \"content\":\"# baselines\\n…\"\n"
                          "}}\n"
                          "```"),
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "name": {"type": "string"},
                                         "mime": {"type": "string"},
                                         "content": {"type": "string"},
                                         "is_binary": {"type": "boolean"}},
                          "required": ["run_id", "name", "content"]}),
        Tool(name="list_run_artefacts",
             description="List all artefacts attached to a run (names + sizes, no content).",
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"}},
                          "required": ["run_id"]}),
        Tool(name="get_run_artefact",
             description="Fetch one artefact's full content.",
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "name": {"type": "string"}},
                          "required": ["run_id", "name"]}),
        Tool(name="delete_run_artefact",
             description="Remove an artefact from a run.",
             inputSchema={"type": "object",
                          "properties": {"run_id": {"type": "string"},
                                         "name": {"type": "string"}},
                          "required": ["run_id", "name"]}),
    ]

    # Dynamic per-resource runners (blocking; poll variants live above).
    dynamic: list[Tool] = []
    for a in agents:
        dynamic.append(Tool(
            name=f"agent_{a['slug'].replace('-', '_')}",
            description=(f"Run agent **{a['name']}** and wait for the result. "
                         f"{a['description']}\n\n"
                         f"**`target_slug` is REQUIRED** — create a Target first with "
                         f"`create_target` if none exists. Calls without it are rejected.\n\n"
                         f"Equivalent to `run_agent_async`(slug={a['slug']!r}) + poll."),
            inputSchema={"type": "object",
                         "properties": {
                             "input": {"type": "string",
                                       "description": "User input passed to the agent."},
                             "target_slug": {"type": "string",
                                             "description": "Slug of the Target this run delivers against. REQUIRED."},
                         },
                         "required": ["input", "target_slug"]},
        ))
    for w in workflows:
        dynamic.append(Tool(
            name=f"workflow_{w['slug'].replace('-', '_')}",
            description=(f"Run workflow **{w['name']}** ({w['kind']}) and wait for "
                         f"completion. {w['description']}\n\n"
                         f"**`target_slug` is REQUIRED** — create a Target first with "
                         f"`create_target` if none exists. Calls without it are rejected.\n\n"
                         f"Equivalent to `run_workflow_async`(slug={w['slug']!r}) + poll. "
                         f"If `graph.max_hops` / `graph.max_tokens` are set, the run "
                         f"may stop gracefully with `output.limit_reached`."),
            inputSchema={"type": "object",
                         "properties": {
                             "input": {"type": "string"},
                             "target_slug": {"type": "string",
                                             "description": "Slug of the Target this run delivers against. REQUIRED."},
                         },
                         "required": ["input", "target_slug"]},
        ))

    return static + dynamic


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _slug_from(prefix: str, name: str) -> str:
    return name[len(prefix):].replace("_", "-")


def _ok(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _err(status: int, text: str) -> list[TextContent]:
    return [TextContent(type="text",
                        text=json.dumps({"error": True, "status": status, "message": text}, indent=2))]


def _caller_run_id(args: dict) -> str | None:
    """Which run is calling us. Prefers the gateway-injected
    ``_gateway_caller_run_id`` (set from the per-run X-Aw-Caller-Run-Id
    header — see api/agents.py::write_run_mcp_config) since this process is a
    single shared subprocess whose own os.environ can't carry per-call
    identity. Falls back to AW_RUN_ID for direct/non-gateway invocations
    (e.g. a local stdio test)."""
    return args.get("_gateway_caller_run_id") or os.environ.get("AW_RUN_ID")


async def _poll_run(c: httpx.AsyncClient, run_id: str, *, timeout_s: int = 900) -> dict:
    for _ in range(timeout_s):
        await asyncio.sleep(1.0)
        run = (await c.get(f"{BASE}/api/runs/{run_id}")).json()
        if run["status"] in ("success", "error", "cancelled"):
            return run
    return {"status": "timeout", "run_id": run_id}


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    args = arguments or {}
    async with httpx.AsyncClient(timeout=120) as c:
        # --- discovery ---
        if name == "list_agents":
            params: dict[str, Any] = {}
            if args.get("include_deleted"):
                params["include_deleted"] = "true"
            if args.get("exclude_pattern"):
                params["exclude_pattern"] = args["exclude_pattern"]
            r = await c.get(f"{BASE}/api/agents", params=params)
            return _ok(r.json())
        if name == "list_workflows":
            params = {}
            if args.get("include_deleted"):
                params["include_deleted"] = "true"
            if args.get("exclude_pattern"):
                params["exclude_pattern"] = args["exclude_pattern"]
            r = await c.get(f"{BASE}/api/workflows", params=params)
            return _ok(r.json())
        if name == "list_agents_deleted":
            r = await c.get(f"{BASE}/api/agents", params={"deleted_only": "true"})
            return _ok(r.json())
        if name == "list_workflows_deleted":
            r = await c.get(f"{BASE}/api/workflows", params={"deleted_only": "true"})
            return _ok(r.json())
        if name == "get_agent":
            r = await c.get(f"{BASE}/api/agents/{args['slug']}")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "get_workflow":
            r = await c.get(f"{BASE}/api/workflows/{args['slug']}")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_models":
            return _ok((await c.get(f"{BASE}/api/models")).json())
        if name == "list_tools":
            return _ok((await c.get(f"{BASE}/api/tools")).json())

        # --- CRUD: agents ---
        if name == "create_agent":
            r = await c.post(f"{BASE}/api/agents", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "update_agent":
            slug = args.pop("slug")
            r = await c.put(f"{BASE}/api/agents/{slug}", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "delete_agent":
            params = {"hard": "true"} if args.get("hard") else {}
            r = await c.delete(f"{BASE}/api/agents/{args['slug']}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "restore_agent":
            r = await c.post(f"{BASE}/api/agents/{args['slug']}/restore")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- CRUD: workflows ---
        if name == "create_workflow":
            r = await c.post(f"{BASE}/api/workflows", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "update_workflow":
            slug = args.pop("slug")
            r = await c.put(f"{BASE}/api/workflows/{slug}", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "delete_workflow":
            params = {"hard": "true"} if args.get("hard") else {}
            r = await c.delete(f"{BASE}/api/workflows/{args['slug']}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "restore_workflow":
            r = await c.post(f"{BASE}/api/workflows/{args['slug']}/restore")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- runs ---
        if name == "run_agent_async":
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            body: dict[str, Any] = {"input": {"input": args["input"]}}
            if args.get("target_id"):
                body["target_id"] = args["target_id"]
            if args.get("target_slug"):
                body["target_slug"] = args["target_slug"]
            if args.get("session_id"):
                body["session_id"] = args["session_id"]
            if args.get("notion_task_id"):
                body["notion_task_id"] = args["notion_task_id"]
            # Chain-depth loop guard: tell the backend which run is dispatching
            # this one — not something the calling LLM sets, see _caller_run_id.
            if _rid := _caller_run_id(args):
                body["caller_run_id"] = _rid
            r = await c.post(f"{BASE}/api/agents/{args['slug']}/run", json=body)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "run_workflow_async":
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            body = {"input": {"input": args["input"]}}
            if args.get("target_id"):
                body["target_id"] = args["target_id"]
            if args.get("target_slug"):
                body["target_slug"] = args["target_slug"]
            if _rid := _caller_run_id(args):
                body["caller_run_id"] = _rid
            r = await c.post(f"{BASE}/api/workflows/{args['slug']}/run", json=body)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "run_monitor_async":
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            if not args.get("command"):
                return _err(400, "command is required")
            body = {"command": args["command"]}
            if args.get("target_id"):
                body["target_id"] = args["target_id"]
            if args.get("target_slug"):
                body["target_slug"] = args["target_slug"]
            if args.get("cwd"):
                body["cwd"] = args["cwd"]
            if args.get("timeout_seconds"):
                body["timeout_seconds"] = args["timeout_seconds"]
            if args.get("label"):
                body["label"] = args["label"]
            if args.get("notion_task_id"):
                body["notion_task_id"] = args["notion_task_id"]
            if _rid := _caller_run_id(args):
                body["caller_run_id"] = _rid
            r = await c.post(f"{BASE}/api/monitor/run", json=body)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "return_to_caller_agent":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify the calling run — this tool only works inside a docker CLI agent run.")
            if args.get("kind") not in ("result", "question", "blocker"):
                return _err(400, "kind is required and must be one of: result, question, blocker")
            r = await c.post(f"{BASE}/api/runs/{own_run_id}/return-to-caller",
                             json={"message": args["message"], "kind": args["kind"]})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "ask_human":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify this run — this tool only works inside a docker CLI agent run.")
            question = (args.get("question") or "").strip()
            if not question:
                return _err(400, "question is required")
            r = await c.post(f"{BASE}/api/telegram/question",
                             json={"run_id": own_run_id, "question": question})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "mark_flow_done":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify this run — this tool only works inside a docker CLI agent run.")
            if args.get("outcome") not in ("success", "partial", "failed"):
                return _err(400, "outcome is required and must be one of: success, partial, failed")
            qa_run_id = args.get("qa_run_id") or None
            qa_not_needed = bool(args.get("qa_not_needed"))
            if qa_run_id and qa_not_needed:
                return _err(400, "pass either qa_run_id or qa_not_needed=true, not both")
            # Neither given is NOT rejected here — the backend auto-resolves qa_run_id
            # from context (same notion_task_id/target_id, most recent succeeded qa-*
            # agent run) before rejecting for real. See core.wakeups.mark_flow_done.
            r = await c.post(f"{BASE}/api/runs/{own_run_id}/mark-done",
                             json={"summary": args.get("summary") or "", "outcome": args["outcome"],
                                   "qa_run_id": qa_run_id, "qa_not_needed": qa_not_needed})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "mark_as_planned":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify this run — this tool only works inside a docker CLI agent run.")
            r = await c.post(f"{BASE}/api/runs/{own_run_id}/mark-planned",
                             json={"summary": args.get("summary") or ""})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "register_callback":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify the calling run — this tool only works inside a docker CLI agent run.")
            run_id = args.get("run_id")
            if not run_id:
                return _err(400, "run_id is required")
            r = await c.post(f"{BASE}/api/runs/{run_id}/register-callback",
                             json={"origin_run_id": own_run_id, "session_id": args.get("session_id")})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "supervise":
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify this run — this tool only works inside a docker CLI agent run.")
            session_id = args.get("session_id")
            if not session_id:
                return _err(400, "session_id is required")
            r = await c.post(f"{BASE}/api/supervisions",
                             json={"caller_run_id": own_run_id, "target_session_id": session_id,
                                   "forever": bool(args.get("forever")), "debounce_s": args.get("debounce_s")})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "stop_supervisor":
            supervision_id = args.get("supervision_id")
            if not supervision_id:
                return _err(400, "supervision_id is required")
            r = await c.post(f"{BASE}/api/supervisions/{supervision_id}/stop")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "supervisor_status":
            if args.get("supervision_id"):
                r = await c.get(f"{BASE}/api/supervisions/{args['supervision_id']}")
                return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
            own_run_id = _caller_run_id(args)
            if not own_run_id:
                return _err(400, "Could not identify this run — this tool only works inside a docker CLI agent run.")
            r = await c.get(f"{BASE}/api/supervisions", params={"caller_run_id": own_run_id})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name in ("run_status", "get_run"):
            params = {"summary": "true"} if args.get("summary") else {}
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "run_events":
            params = {}
            if args.get("after_ts"):
                params["after_ts"] = args["after_ts"]
            if args.get("kinds"):
                params["kinds"] = args["kinds"]
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/events", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "run_tree":
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/tree")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_runs":
            params = {"limit": str(args.get("limit") or 20), "summary": "true"}
            if args.get("status"):
                params["status"] = args["status"]
            r = await c.get(f"{BASE}/api/runs", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "get_run_detail":
            run_id = args["run_id"]
            r = await c.get(f"{BASE}/api/runs/{run_id}")
            if r.status_code != 200:
                return _err(r.status_code, r.text)
            run = r.json()
            ev_params = {"limit": str(args.get("events_limit") or 500)}
            er = await c.get(f"{BASE}/api/runs/{run_id}/events", params=ev_params)
            run["events"] = er.json() if er.status_code == 200 else []
            return _ok(run)
        if name == "list_bots":
            scan_limit = str(args.get("scan_limit") or 500)
            r = await c.get(f"{BASE}/api/runs",
                             params={"limit": scan_limit, "kind": "agent", "summary": "true"})
            if r.status_code != 200:
                return _err(r.status_code, r.text)
            runs = r.json()  # already ordered most-recent-first
            bots: dict[str, dict] = {}
            for run in runs:
                bot = run.get("source_slug")
                if not bot or bot in bots:
                    continue
                sid = run.get("session_id")
                if not sid:
                    continue
                bots[bot] = {"bot": bot, "session_id": sid, "renamed_name": None, "runs": []}
            for bot, row in bots.items():
                sr = await c.get(f"{BASE}/api/sessions/{row['session_id']}")
                if sr.status_code == 200:
                    row["renamed_name"] = sr.json().get("name") or None
                rr = await c.get(f"{BASE}/api/runs",
                                  params={"session_id": row["session_id"], "limit": "500", "summary": "true"})
                if rr.status_code == 200:
                    row["runs"] = [{"run_id": rn["id"], "status": rn["status"]} for rn in rr.json()]
            return _ok(list(bots.values()))
        if name == "list_sessions":
            params = {"limit": str(args.get("limit") or 100)}
            if args.get("source_slug"):
                params["source_slug"] = args["source_slug"]
            if args.get("q"):
                params["q"] = args["q"]
            r = await c.get(f"{BASE}/api/sessions", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "wait_run":
            # Use a separate client with a larger timeout — the server itself
            # waits up to timeout_s, and we need our HTTP read budget to cover it.
            timeout_s = int(args.get("timeout_s") or 300)
            params = {"timeout_s": str(timeout_s)}
            if args.get("poll_interval_s") is not None:
                params["poll_interval_s"] = str(args["poll_interval_s"])
            if args.get("max_cost_usd") is not None:
                params["max_cost_usd"] = str(args["max_cost_usd"])
            if args.get("summary"):
                params["summary"] = "true"
            async with httpx.AsyncClient(timeout=timeout_s + 30) as wc:
                r = await wc.get(f"{BASE}/api/runs/{args['run_id']}/wait", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "peek_run_output":
            params = {}
            if args.get("tail_events"):
                params["tail_events"] = str(args["tail_events"])
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/peek", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "run_agents_parallel":
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            r = await c.post(f"{BASE}/api/runs/parallel", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "cancel_run":
            r = await c.post(f"{BASE}/api/runs/{args['run_id']}/cancel")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name in ("clear_session", "compact_session"):
            sid = (args.get("session_id") or "").strip()
            if not sid:
                return _err(400, "session_id is required — get it with `echo $AW_SESSION_ID`.")
            command = "clear" if name == "clear_session" else "compact"
            r = await c.post(f"{BASE}/api/sessions/{sid}/pending-command",
                             json={"command": command})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "rename_session":
            sid = (args.get("session_id") or "").strip()
            if not sid:
                return _err(400, "session_id is required — get it with `echo $AW_SESSION_ID`.")
            r = await c.patch(f"{BASE}/api/sessions/{sid}", json={"name": args.get("name", "")})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "cancel_all_runs":
            r = await c.post(f"{BASE}/api/runs/cancel_all")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Targets ---
        if name == "list_callers":
            params = {}
            if args.get("source"):
                params["source"] = args["source"]
            if args.get("meta_filter"):
                params["meta_filter"] = args["meta_filter"]
            r = await c.get(f"{BASE}/api/callers", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_caller_messages":
            params = {"source": args["source"], "external_id": args["external_id"]}
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/callers/messages", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_targets":
            params = {}
            if args.get("include_deleted"):
                params["include_deleted"] = "true"
            if args.get("status"):
                params["status"] = args["status"]
            if args.get("q"):
                params["q"] = args["q"]
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/targets", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "get_target":
            r = await c.get(f"{BASE}/api/targets/{args['slug']}")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "create_target":
            r = await c.post(f"{BASE}/api/targets", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "update_target":
            slug = args.pop("slug")
            r = await c.put(f"{BASE}/api/targets/{slug}", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "delete_target":
            params = {"hard": "true"} if args.get("hard") else {}
            r = await c.delete(f"{BASE}/api/targets/{args['slug']}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "restore_target":
            r = await c.post(f"{BASE}/api/targets/{args['slug']}/restore")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "target_summary":
            r = await c.get(f"{BASE}/api/targets/{args['slug']}/summary")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_target_runs":
            params = {}
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/targets/{args['slug']}/runs", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "link_run_to_target":
            slug = args.pop("slug")
            r = await c.post(f"{BASE}/api/targets/{slug}/link_run", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "unlink_run_from_target":
            slug = args["slug"]; rid = args["run_id"]
            params = {}
            if args.get("include_descendants"):
                params["include_descendants"] = "true"
            r = await c.delete(f"{BASE}/api/targets/{slug}/link_run/{rid}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "attach_pr_to_target":
            slug = args.pop("slug")
            r = await c.post(f"{BASE}/api/targets/{slug}/pr", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "detach_pr_from_target":
            slug = args["slug"]; url = args["url"]
            r = await c.delete(f"{BASE}/api/targets/{slug}/pr", params={"url": url})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Target Lessons ---
        if name == "search_lessons":
            params = {}
            for k in ("tags", "category", "q"):
                if args.get(k):
                    params[k] = args[k]
            if args.get("include_superseded"):
                params["include_superseded"] = "true"
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/lessons/search", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_target_lessons":
            slug = args["slug"]
            params = {}
            if args.get("include_superseded"):
                params["include_superseded"] = "true"
            r = await c.get(f"{BASE}/api/targets/{slug}/lessons", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "create_target_lesson":
            slug = args.pop("slug")
            r = await c.post(f"{BASE}/api/targets/{slug}/lessons", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "update_target_lesson":
            slug = args.pop("slug"); lesson_id = args.pop("lesson_id")
            r = await c.put(f"{BASE}/api/targets/{slug}/lessons/{lesson_id}", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "delete_target_lesson":
            slug = args["slug"]; lesson_id = args["lesson_id"]
            params = {"hard": "true"} if args.get("hard") else {}
            r = await c.delete(f"{BASE}/api/targets/{slug}/lessons/{lesson_id}", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Lesson Applications / Metrics / Forecast ---
        if name == "record_lesson_application":
            r = await c.post(f"{BASE}/api/lessons/applications", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_lesson_applications":
            params = {}
            for k in ("lesson_id", "target_id", "outcome"):
                if args.get(k):
                    params[k] = args[k]
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            r = await c.get(f"{BASE}/api/lessons/applications", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "lesson_effectiveness":
            r = await c.get(f"{BASE}/api/lessons/{args['lesson_id']}/effectiveness")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "lesson_forecast":
            r = await c.post(f"{BASE}/api/lessons/forecast", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "lessons_metrics":
            r = await c.get(f"{BASE}/api/lessons/metrics")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Retro scores (Wave-6 A3) ---
        if name == "list_retro_scores":
            params = {}
            if args.get("include_superseded"):
                params["include_superseded"] = "true"
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/retro-scores", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "override_retro_score":
            run_id = args.pop("run_id")
            dimension = args.pop("dimension")
            r = await c.patch(f"{BASE}/api/runs/{run_id}/retro-scores/{dimension}", json=args)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "recompute_retro_score":
            r = await c.post(f"{BASE}/api/runs/{args['run_id']}/retro-scores/recompute")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "get_retro_score_weights":
            r = await c.get(f"{BASE}/api/retro-score-weights")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "set_retro_score_weights":
            r = await c.put(f"{BASE}/api/retro-score-weights", json={"weights": args["weights"]})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_pending_lessons":
            params = {}
            if args.get("limit"):
                params["limit"] = str(args["limit"])
            if args.get("offset"):
                params["offset"] = str(args["offset"])
            r = await c.get(f"{BASE}/api/lessons/pending", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "approve_pending_lesson":
            r = await c.post(f"{BASE}/api/lessons/{args['lesson_id']}/approve")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "archive_lesson":
            r = await c.post(f"{BASE}/api/lessons/{args['lesson_id']}/archive")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Lesson consolidation (Wave-6 L2) ---
        if name == "consolidate_lessons":
            body: dict = {"lesson_ids": args["lesson_ids"], "title": args["title"], "content": args["content"]}
            for k in ("category", "applicable_tags", "confidence", "target_id"):
                if args.get(k) is not None:
                    body[k] = args[k]
            r = await c.post(f"{BASE}/api/lessons/consolidate", json=body)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "suggest_lesson_consolidation":
            params: dict = {}
            for k in ("min_overlap", "limit", "min_cluster_size"):
                if args.get(k) is not None:
                    params[k] = str(args[k])
            r = await c.get(f"{BASE}/api/lessons/consolidate/suggestions", params=params)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "draft_consolidated_lesson":
            r = await c.post(f"{BASE}/api/lessons/consolidate/draft", json={"lesson_ids": args["lesson_ids"]})
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- Run artefacts ---
        if name == "add_run_artefact":
            body = {k: v for k, v in args.items() if k != "run_id"}
            r = await c.post(f"{BASE}/api/runs/{args['run_id']}/artefacts", json=body)
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "list_run_artefacts":
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/artefacts")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "get_run_artefact":
            r = await c.get(f"{BASE}/api/runs/{args['run_id']}/artefacts/{args['name']}")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())
        if name == "delete_run_artefact":
            r = await c.delete(f"{BASE}/api/runs/{args['run_id']}/artefacts/{args['name']}")
            return _err(r.status_code, r.text) if r.status_code != 200 else _ok(r.json())

        # --- dynamic per-resource runners (blocking) ---
        if name.startswith("agent_"):
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            slug = _slug_from("agent_", name)
            body: dict[str, Any] = {"input": {"input": args.get("input", "")}}
            if args.get("target_slug"):
                body["target_slug"] = args["target_slug"]
            if args.get("target_id"):
                body["target_id"] = args["target_id"]
            r = await c.post(f"{BASE}/api/agents/{slug}/run", json=body)
            if r.status_code != 200:
                return _err(r.status_code, r.text)
            run = await _poll_run(c, r.json()["run_id"])
            return _ok(run)
        if name.startswith("workflow_"):
            if not args.get("target_slug") and not args.get("target_id"):
                return _err(400, "target_slug is required. Use list_targets to find an existing Target or create_target to make one, then pass target_slug.")
            slug = _slug_from("workflow_", name)
            body = {"input": {"input": args.get("input", "")}}
            if args.get("target_slug"):
                body["target_slug"] = args["target_slug"]
            if args.get("target_id"):
                body["target_id"] = args["target_id"]
            r = await c.post(f"{BASE}/api/workflows/{slug}/run", json=body)
            if r.status_code != 200:
                return _err(r.status_code, r.text)
            run = await _poll_run(c, r.json()["run_id"])
            return _ok(run)

        return _err(404, f"unknown tool: {name}")


# ---------------------------------------------------------------------------
# Hand-rolled stdio JSON-RPC transport (see the Tool/TextContent/_NoopServer
# note near the top) — same shape as every other src/mcp/*.py server in
# this project. Calls the SDK-decorated-but-plain _list_tools()/_call_tool()
# functions above directly instead of going through mcp.server.Server.
# ---------------------------------------------------------------------------

def _tool_result(req_id, contents: list[TextContent]) -> dict:
    text = contents[0].text if contents else ""
    is_error = False
    try:
        parsed = json.loads(text)
        is_error = isinstance(parsed, dict) and parsed.get("error") is True
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


async def handle_request(request: dict) -> dict | None:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agents-platform-runners", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        tools = await _list_tools()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                    for t in tools
                ]
            },
        }

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            contents = await _call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, not a crash
            return _tool_result(req_id, [TextContent(type="text", text=f"{type(exc).__name__}: {exc}")])
        return _tool_result(req_id, contents)

    return None


async def _async_main() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = await handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
