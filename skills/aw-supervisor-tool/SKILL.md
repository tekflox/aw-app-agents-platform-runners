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

### Idempotent by CHAIN, not just by session

Arming on a second hop of a chain you are already watching does not create
a second watcher. Before inserting, `supervise`/`supervise_card` expand the
supervision you are ASKING for through the same
`_batch_discover`/`_expand_chain` machinery the ticker uses, and compare it
against every `active` supervision **your own session** already armed. Any
overlap — a shared session, a shared run, or a shared Kanban card — and you
get that supervision back instead:

```json
{"ok": true, "supervision_id": "<the existing one>", "existing": true,
 "joined_existing_chain": true, "matched_on": "session|run|notion_task_id",
 "matched_value": "<the id that matched>", "watching_session_id": "<its target>",
 "note": "... already covered ... no new supervision was armed."}
```

`ok:true`, HTTP 200 — **informational, never a failure**. You are still
watched; the requested hop is folded into that supervision's `discovered`
on the spot. `joined_existing_chain:false` on the plain same-session reuse
keeps the two distinguishable.

Why it exists (2026-09-14): one Telegram session armed 3 `forever`
supervisions over one card's delivery chain, seeded at the Architect
(twice) and Product Owner hops. Nothing linked them by `parent_run_id` —
only the shared card. All 3 discovered identical membership and all 3 fired
within 20s of each other: three ~$11 wakeups for one event.

Three things it deliberately will NOT do:

- **Absorb across callers.** A wakeup is delivered to the CALLER's session,
  so folding agent B's `supervise` into agent A's watcher would mean B is
  never woken at all. Only your own supervisions are candidates.
- **Absorb into a `waiting_retrigger` row.** That one has stopped watching
  the target chain and only observes your session to decide
  retrigger-vs-give-up — you would get a watcher that is not watching.
- **Downgrade your guarantee.** A `forever=true` request is never absorbed
  into a one-shot watcher (and the existing one is never silently upgraded).

Known open edge: the card frontier is transitive, so a long-lived `forever`
watcher that has accumulated many cards can absorb a genuinely new
`supervise` call into a much larger chain, whose "idle" is later and rarer
than you wanted. `stop_supervisor` + re-arm if that bites.

### Coalesced against your own `call_me_back`

The chain dedupe above only sees other `Supervision` rows. The other way you
get told twice is not a supervision at all: `run_agent_async(call_me_back=
true)` **plus** `supervise` on the same session. Both report that run
finishing, and the duplicate lives as columns on the dispatched `Run`
(`call_me_back` / `callback_run_id` / `callback_origin_run_id` /
`callback_target_session_id`), which no chain comparison can reach.

Why it exists (2026-09-16): one session rolling `security-scan.yml` out to 50
repos called both, per repo. Nine `forever` supervisions armed between 06:16
and 08:00, ten wakeups delivered onto a session whose wakeup runs cost $2–8
each.

The coalescing happens at **delivery** time, in `_process_one`: when the run
that tipped the chain into all-terminal (`_last_ended` — the same run
`_reason_key` keys `finished` on) already woke THIS caller through its own
callback, the supervision's wakeup is suppressed. `suppressed_count` goes up
and the `wakeup_history` entry carries `"suppressed_by": "call_me_back"`,
distinguishing it from `"debounce"`.

Four things it deliberately will NOT suppress — each one a case where nobody
actually told you:

- **Anything but `finished`.** A callback fires on terminal, so a chain
  blocked on `ask_human` has produced none at all; `waiting_human` would be
  the most actionable reason you could lose. `error`/`idle_timeout` are rare,
  and being told twice about a failure is the cheap direction to be wrong in.
- **A callback that never landed.** The pivot is `Run.callback_run_id` (the
  run the callback actually created), never `callback_done` — that one flips
  *before* delivery, and every dead-end branch in `_watch_and_callback` marks
  it done and returns without waking anyone. Suppressing on it would strip
  the backstop from exactly the already-broken cases.
- **A callback redirected elsewhere.** `call_me_back_on` sends the wake-up to
  a third session; you were told nothing.
- **A run further down the chain.** Only the run you armed the callback on is
  covered. Everything it fans out to is still watched — which is the thing a
  `call_me_back` can't do, and why arming both is reasonable rather than a
  mistake.

Arm time is informational only. If a matching outstanding callback exists,
`supervise` adds `"callback_overlap": {"run_ids": [...], "note": "..."}` to
its response — and arms the supervision anyway. Refusing there would break
`_join_existing_chain`'s own rule against handing back a weaker guarantee: at
arm time the chain is one just-dispatched run, and whether it fans out or
self-continues is not knowable yet. In the trace above it did both.

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
1. A run with `initiator_kind="wakeup"` **on the caller's own session** never
   counts as chain activity.
2. The caller's own session is never added to the watched set while
   `forever=true` (only observed in one-shot's post-wakeup retrigger check).

Invariant 1 used to read "any run with `initiator_kind='wakeup'`", full stop.
That over-matched: a coder that dispatches sub-work with `call_me_back` runs
its own continuations on its own — *watched* — session, also with
`initiator_kind="wakeup"`. The supervisor discarded them and read a working
coder as idle, delivering `finished` while a run in the chain was still
`running` and re-firing on each new "idle" plateau (three wakeups for one
coder, 2026-09-16). A supervisor wakeup landing on a watched session belongs
to some OTHER supervision and is genuine work; only your own delivery to your
own session is self-trigger. The same narrowing applies to `_last_ended`,
which decides the chain's reported outcome.

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
- `repos/aw-app-agents-platform-runners/agents_platform_runners_app/mcp_server.py`
  — the `Tool()` definitions + dispatch, same shape as `register_callback`
  (`supervise` is at ~line 770). This is the ONLY copy: the `mcp_server/
  agent_mcp.py` this file used to point at, in agents-platform-multitenant,
  no longer exists. That app repo is also where this skill itself lives —
  `/opt/aw-workspace/skills/aw-supervisor-tool/` is a generated mirror, never
  edit it, and a change here needs an app reinstall to reach agents.
- `backend/tests/test_supervisor.py` — activity/reason rules, cycle
  discovery, atomic claim under a race, one-shot give-up/retrigger,
  forever re-arm, and the `call_me_back` coalescing (delivery-time
  suppression plus each case that must still fire). The schema-pin test for
  the tools went with `mcp_server/agent_mcp.py`.
  `backend/tests/test_callback_run_id_recorded.py` pins the other half:
  `_watch_and_callback` records `callback_run_id` on a delivered callback and
  leaves it NULL on a failed one.

## Sibling primitive, not a replacement

`register_agent_callback` (`core.wakeups`) is a **level-trigger** on ONE
run's own completion — this is an **edge-trigger** on a whole chain's
idleness. Don't reach for one to build the other: a callback cannot tell you
about the sub-work that run dispatches, and a supervision cannot tell you
about a run that never goes quiet.

They are no longer merely pattern-siblings, though. Since 2026-09-16 the
supervisor READS the callback's own columns on the dispatched `Run` and
coalesces its `finished` wakeup against a delivered callback (see "Coalesced
against your own `call_me_back`" above), and `_watch_and_callback` writes
`Run.callback_run_id` specifically so the supervisor can distinguish a
delivered callback from an attempted one. The delivery paths are still
separate — `_deliver_wakeup` reuses `wakeups._rerun_and_deliver`, and
nothing else crosses — but a change to when a callback fires, or to what
`callback_done`/`callback_run_id` mean, now changes when supervisions stay
silent. Read both before touching either.

A third sibling, `run_monitor_async` (`core.monitor_run`, see the `aw-agents`
skill), is level-trigger like `register_agent_callback` but for a RAW SHELL
COMMAND with no agent/LLM in it at all — use that (not this Supervisor tool,
and not the harness's own flaky `Monitor` tool) when you just need to watch
a long-running command and get woken on its exit code.
