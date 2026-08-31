---
name: aw-supervisor-tool
description: The Supervisor tool — 4 MCP tools (supervise/stop_supervisor/supervisor_status/list_supervisors) any Agents Platform agent can call to watch another session's whole run chain and get woken up exactly once when it goes idle, plus list and turn off supervisions armed by ANY session. Mechanism, not a persona — use when an agent needs to watch a delegated task without polling, when asked what is supervising what or to stop a supervisor, or when debugging/extending core/supervisor.py in repos/agents-platform/backend.
---

# aw-supervisor-tool — watch a session, get woken when it stops

A profile-agnostic mechanism, not a "Manager" persona: any agent can
supervise any other session. It reports STATE only (who ran, how long, why
it stopped) — deciding whether that's good or bad is on the calling agent.

## The 4 tools

```
supervise(session_id, forever=false, debounce_s?) → {supervision_id, existing: bool}
stop_supervisor(supervision_id | target_session_id | notion_task_id | caller_session_id) → {ok, stopped:[ids], count}
supervisor_status(supervision_id?)
  → without arg: your own supervisions [{supervision_id, target_session_id, status, forever, wakeup_count}]
  → with arg: full detail {status, edge_state, discovered, last_activity_at, idle_since, stop_reason, wakeups}
list_supervisors(status="armed", target_session_id?, caller_session_id?, limit=100)
  → EVERY supervision on the deployment, whoever armed it
```

### Scope: `supervisor_status` is yours, `list_supervisors` is everyone's

`supervisor_status` resolves your own session from `caller_run_id` and only
ever shows what YOU armed. That's the right default for an agent managing
its own delegations — and it's also why a stray `forever` supervision was
historically invisible: nobody could see it but its own caller, who by
definition wasn't asking. Enumerating meant sweeping `/api/runs` and
re-querying `/api/supervisions?caller_run_id=…` once per distinct session
(545 of them, to surface 4 live watchers, 2026-08-25).

`list_supervisors` is that sweep as one call. Default `status="armed"`
(active + waiting_retrigger — the ones that can still fire); pass
`status=null` for the full history including `done`/`stopped`. Each row
carries both sides resolved to an agent slug (`caller_agent_slug` /
`target_agent_slug`), so you can read "watch-sonnet is watching
telegram-opus" without a second lookup.

### Turning one off when you don't own it

`stop_supervisor` takes exactly one of three selectors:

| Selector | Stops |
|---|---|
| `supervision_id` | that one supervision (the original form) |
| `target_session_id` | every armed supervision WATCHING that session |
| `notion_task_id` | every armed supervision watching that CARD |
| `caller_session_id` | every armed supervision that session ARMED |

The session forms exist because the id is precisely what a third party
never had. "Turn off whatever is supervising session X" is
`target_session_id`; "stop this agent from watching anything" is
`caller_session_id`. Neither is restricted to your own session, and both
skip already-terminal rows rather than rewriting them. Run
`list_supervisors` first — the stop is not reversible, a stopped
supervision has to be re-armed with `supervise`.

`session_id`/`supervision_id` are the only params you pass — `caller_run_id`
(and from it, your own session_id) comes free via the MCP layer's
`_caller_run_id` (same mechanism `run_agent_async`'s chain-depth guard
uses). Idempotent: calling `supervise` again for the same target session
returns the existing supervision instead of arming a duplicate.

Rejected if `session_id` doesn't exist, or if it's your own session
(guaranteed self-trigger loop).

## What "the chain" means

Not just the target session — every descendant session/run it spawns
(discovered via `parent_run_id`, same edge `run_tree` uses, but via batched
queries instead of that endpoint's recursive N+1 walk). Idle = no
`pending`/`queued`/`running` run anywhere in the chain for 60s straight.

## Wakeup delivery

A run on YOUR OWN session, `initiator_kind="wakeup"`, prompt containing a
JSON payload:

```json
{"supervision_id": "...", "reason": "finished|waiting_human|error|idle_timeout",
 "sessions": [{"session_id": "...", "agent_slug": "...",
   "runs": [{"run_id": "...", "status": "...", "duration_s": 123}]}],
 "pending_human_questions": [{"question": "...", "run_id": "...", "created_at": "..."}]}
```

`reason` priority: `waiting_human` (a pending `HumanQuestion` exists) >
`error` (some run in the chain ended in error) > `finished` (all known runs
succeeded) > `idle_timeout` (generic fallback — chain went quiet, no other
signal).

## Two modes

- **One-shot (default, `forever=false`)**: after the wakeup, the
  supervision stops watching the old chain and instead watches only YOUR
  session for 60s. Dispatch something new in that window → resumes full
  tracking of the original chain. Nothing new → gives up (`status="done"`).
- **`forever=true`**: keeps delivering one wakeup per running→idle
  transition, indefinitely, until you call `stop_supervisor`. The edge only
  re-arms once the chain regains a non-terminal run.

## Anti-self-trigger rule (why you can't watch your own session)

Delivering a wakeup creates a run on the caller's own session. In `forever`
mode that run would otherwise look like fresh "activity" in the watched
chain, re-arming the edge and firing again the moment it too goes idle —
infinite loop. Two invariants prevent it, both enforced in
`core/supervisor.py`, never opt-out:
1. Runs with `initiator_kind="wakeup"` never count as chain activity.
2. The caller's own session is never added to the watched set while
   `forever=true` (only observed in one-shot's post-wakeup retrigger check).

## Implementation (agents-platform's own backend, not this workspace's)

This mechanism lives inside the `agents-platform-multitenant` service
itself — reachable from here through the gateway's
`aw__agents_platform_runners__*` tool namespace, not something this workspace's own code implements. Useful
if you're debugging or extending the mechanism, not for using it day to day:

- `app/models.py::Supervision` — the persisted table. `status` (indexed:
  `active|waiting_retrigger|done|stopped`) is what boot pickup queries
  (`WHERE status IN ('active','waiting_retrigger')`). `edge_state`
  (`active|idle_pending_wakeup|idle_wakeup_delivered`) is the actual
  edge-trigger state machine.
- `app/core/supervisor.py` — everything else:
  - `create_supervision`/`stop_supervision`/`list_caller_supervisions`/
    `get_supervision_detail` — the 4 operations the API layer calls.
  - `supervisor_ticker()` — ONE asyncio task (armed in `main.py`'s
    lifespan next to the wakeup/callback rearms), ticking every 10s. Not
    one task per supervision.
  - `_batch_discover`/`_expand_chain` — chain discovery in ONE combined
    query per tick across every active supervision (never
    `/runs/{id}/tree`, whose `collect()` is O(N) queries for an N-node
    tree). Cycles (e.g. `return_to_caller_agent` producing an A→B→A run
    chain) are deduped by run id + session id, same as `run_tree`'s own
    `seen` set.
  - `_claim_edge` — literal copy of `core.wakeups._mark_callback_done`'s
    atomic compare-and-set shape: `idle_pending_wakeup` →
    `idle_wakeup_delivered` only succeeds once, so a boot pickup can never
    re-deliver a wakeup already sent before a restart.
  - `_deliver_wakeup` — reuses `wakeups._rerun_and_deliver`/
    `_resolve_channel` verbatim. Do not invent a second delivery path.
- `app/api/supervisions.py` — thin REST layer the MCP tools call
  (`POST /api/supervisions`, `POST /{id}/stop`, `POST /stop` (by session),
  `GET /api/supervisions`, `GET /all`, `GET /{id}`). The two fixed paths
  (`/stop`, `/all`) MUST stay declared before their `/{supervision_id}`
  siblings or FastAPI matches the literal as an id.
- `mcp_server/agent_mcp.py` — the 4 `Tool()` definitions + dispatch, same
  shape as `register_callback`. **This file is not what runs**: the gateway
  spawns a vendored copy at
  `repos/aw-app-agents-platform-runners/agents_platform_runners_app/mcp_server.py`.
  Edit both, byte-identical, or the schema you ship is not the schema
  agents see.
- `backend/tests/test_supervisor.py` — activity/reason rules, cycle
  discovery, atomic claim under a race, one-shot give-up/retrigger,
  forever re-arm, and a schema-pin test for the 3 tools (imports
  `mcp_server.agent_mcp` directly — that package lives at the repo root,
  not under `backend/`).

## Sibling primitive, not a replacement

`register_agent_callback` (`core.wakeups`) is a **level-trigger** on ONE
run's own completion — this is an **edge-trigger** on a whole chain's
idleness. They share only the atomic-claim and boot-rearm *patterns*, not
any code path. Don't reach for one to build the other.

A third sibling, `run_monitor_async` (`core.monitor_run`, see the `aw-agents`
skill), is level-trigger like `register_agent_callback` but for a RAW SHELL
COMMAND with no agent/LLM in it at all — use that (not this Supervisor tool,
and not the harness's own flaky `Monitor` tool) when you just need to watch
a long-running command and get woken on its exit code.
