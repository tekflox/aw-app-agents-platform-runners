---
name: aw-agents-dispatch-reliability
description: What to do when a run you dispatched through this app misbehaves — a fan-out that stalls on an MCP approval gate nobody can approve, a run that died in transport versus one that died holding real work, a scheduled agent that has been failing silently at setup, a delegate whose turn ended on a passive wait, and the same completion arriving several times through independent channels. Read this before retrying, redispatching, or acting on a dispatched run's self-report.
---

# aw-agents-dispatch-reliability — reading a dispatched run correctly

Dispatching work (`run_agent_async`, `run_agents_parallel`,
`run_monitor_async`, the internal scheduler) has failure modes that all
look like the same thing from outside: nothing came back, or something came
back that isn't what it appears to be. Each one below has a different
correct response, and picking the wrong one is expensive — redispatching a
run that already committed code duplicates work, and retrying a structural
mismatch burns the whole batch.

This is the orchestrator's side of the job. The delegate-side rules live in
the `aw-agent-coder` / `aw-agent-qa` / `aw-agents-flow` contracts.

## Pick the right fan-out mechanism for MCP-gated work

A fan-out of N independent tasks that each call a **permission-gated MCP
tool** can stall indefinitely on the approval prompt. Dispatched runs
execute unattended, so there is nobody present to approve, and the gate was
designed for an interactive approver. Observed live: a batch of ten
read-only document tasks dispatched via `run_agents_parallel` — most of the
batch sat waiting on an approval nobody could give.

**Switch mechanism, not retry count.** Dispatch that class of fan-out as
native in-session subagents (your CLI's own `Agent` tool) instead: a
subagent launched that way inherits the parent session's already-granted
tool permissions and never re-triggers the prompt.

- Applies to any fan-out whose tasks call an MCP tool behind an approval
  gate. Not every MCP tool is gated — if you're unsure, watch whether a
  single such call already prompted earlier in this session.
- Does **not** apply to fanning out Coder/QA/Architect work that doesn't
  touch a gated tool. That dispatch class is unaffected and this app's
  parallel dispatch remains the right tool for it.
- Don't debug the gate config or raise the retry count. This is a
  structural mismatch between unattended execution and an interactive
  gate, not a flaky failure.

Note the trade-off this buys you: in-process subagents are tied to the
parent session's lifetime (see "a success status does not mean the
sub-work reported" below), so use them for work whose result you consume
in the same turn, not for work you intend to walk away from.

## How to read a dead run — two opposite shapes, one signature

A run that ends `status=error` with "no activity for 600s — the producer
appears to have died" is **not** self-evidently a transport blip. Two
different failures produce a similar-looking dead run, and they call for
opposite responses. Decide which one you're in **before** doing anything:

```
get_run_detail(run_id=...)   # or run_events / get_run
```

| What the trace shows | What happened | What to do |
|---|---|---|
| Zero tool calls, zero assistant turns; error text names the transport (`runner '<name>' /execute returned 404`, or a dispatch-layer 401) | The run died in transport before any LLM turn. Nothing ran. | Redispatch with the same input. |
| Real tool calls, file edits, commits, or a completed verdict before the death timestamp | Real work landed server-side, then the producer died. | Do **not** redispatch. Recover it. |

### The transport-death case

Just redispatch — same prompt, same target. A dispatch-layer **401 is the
same class of transient failure as a 404**; neither is specific to
`run_agent_async`, and `run_monitor_async` shows the same signature.

- Do not treat a "rejected"/"failed" wording in a stale run as a real QA
  finding to go fix in the code. The delivery was never reviewed.
- Do not escalate to a human as if the delivery is broken.
- **Verify the retry actually started** — check `run_status` right after
  and confirm `running`, not an instant error. The arming call returning
  success is not proof.
- If it errors instantly a second time, stop retrying that mechanism and
  fall back to blocking directly in-turn for that one wait.
- **Three or four occurrences in the same short window is not a blip.**
  That's a real runner-transport reliability problem and belongs on a card
  rather than being silently absorbed again. This has been under-escalated
  before: several sessions each correctly recognised the signature, each
  retried, and none of them raised it — the threshold is there precisely
  because recognising it correctly every time still leaves the underlying
  fault unfixed.

### The real-work-landed case

The dead run's own final self-report is never coming — it's dead. Its
**event trace** is the ground truth instead. One recovery of this kind
rescued over a thousand lines of committed work from a run that technically
ended in `error`.

1. `get_run_detail` / `run_events` on the dead `run_id` and walk the trace
   up to the death point: which files were edited or committed, what a QA
   pass actually checked and concluded, what the last known-good state was.
2. When you report upward, label it explicitly as **recovered provenance
   from a dead run's event trace** — not your own fresh review, and not the
   sub-agent's completed self-report. That distinction matters if anyone
   later has to audit who actually verified what.
3. Only then decide whether the *remaining* work needs a fresh dispatch,
   and scope that dispatch to what is actually missing rather than the
   whole task again.

**Prefer resuming the session over reconstructing the trace when the crash
was at the container/transport level** rather than a fully torn-down run:
call `run_agent_async` with the **same `session_id`** and ask the agent to
check whether its own uncommitted working-tree changes survived. A live
agent inspecting its own filesystem is more reliable than inferring state
from an event trace, and it can finish the remaining work in the same call.
Fall back to the trace reconstruction above when resuming isn't possible or
doesn't come back.

## A quiet scheduled agent is not automatically a clean result

If a cron-dispatched agent's own last-run marker is older than its expected
cadence, don't default to "nothing new to report." The scheduler can fail
**at setup**, every time, for every slot in the gap — producing no session,
no Run row, no card, no message: nothing a human would ever stumble on.

Root cause of the confirmed instance: the platform's internal scheduler
calls `run_agent()` directly for cron dispatch **with no workspace**,
unlike this app's MCP tools, which inject the calling workspace's slug into
every dispatch server-side. An agent whose own `Agent.workspace` is NULL,
dispatched through that unprotected path, fails at setup before a usable
Run or session exists. The MCP tools paper over it on every
manually-triggered call — which is exactly why a human asking for a manual
run sees nothing wrong. **Only the unattended cron path is bitten.**

Known-fixed in the data (the affected agent rows were given an explicit
workspace); **the scheduler path itself is still unprotected.** Don't read
"those agents work now" as "this can't happen again."

To check any scheduled agent that looks quiet:

1. Compare its own state/marker against its expected cadence. A gap longer
   than the interval with no error anywhere visible is the tell — not proof
   of a clean idle period.
2. Read the agent's `workspace` field (`GET /api/agents/<slug>`, see
   `aw-agents-platform-rest-api`). `workspace: null` on a tenant with more
   than one registered workspace is the smoking gun.
3. Cross-check `/api/runs?agent_slug=<slug>` over the gap window. A total
   absence of Run rows — not even failed ones — confirms the failure is
   happening before Run-row creation, which rules out "it ran and found
   nothing."
4. Fix by setting the workspace explicitly via the GET+PUT recipe in
   `aw-agents-platform-rest-api`. `PATCH` returns 405; don't spend a
   round-trip on it.

If you *are* a scheduled agent reading this and your own last-run gap looks
larger than expected, run step 2 on yourself before reporting a clean idle
period.

## Driving and distrusting a delegate

A delegated run's claim about its own state can be wrong — stale,
overconfident, or fabricated outright. Verify against something the
delegate doesn't control before you act on it.

### "Done" is a claim, not a result

Before acting on a delegate's `done`/`failed`/`need_human` self-report —
especially one with real consequences, like closing a card, waking a human,
or building on the claimed state — spend one round-trip confirming it
against ground truth: `git log`/`git status` on the actual worktree, a
direct database query, a live request against the endpoint in question.

Observed failures of exactly this kind: two consecutive runs reporting
success to the platform having never committed, pushed, or reindexed
anything; a card escalated to Need Human over an alarming-looking git
divergence that turned out to be an orphaned branch which had never landed
(caught by the orchestrator running `git fetch` and
`git merge-base --is-ancestor` itself, and closed as Done instead of waking
anyone); a reported row count that didn't survive being checked against the
database.

### A self-reported capability list can be pure invention

When a delegate's task is to report **what tools or MCP servers it can
see**, don't accept the verbal answer. One such report named tools that did
not exist, including a namespace a fix was supposed to have removed — the
run's trace showed **zero tool-call events**: the model answered from its
system prompt with no introspection at all and invented plausible names
rather than admitting it hadn't checked.

Force a real tool call (have it actually invoke something from the surface
in question) and check the trace (`get_run_detail`/`run_events`) for
genuine `tool_call` events backing the claim. No corresponding calls means
unverified, however specific and confident it reads — fall back to
mechanistic evidence instead, e.g. the gateway's own traffic logs for
whether that namespace ever fired.

### Don't let a delegate's turn end on a passive wait

A delegate can end its turn on "I'll wait for the CI run to finish" or
"I'll check back once the deploy completes" having called nothing that
blocks or schedules a wakeup. That turn simply ends. Nothing wakes it, and
you're left watching a session that isn't doing anything.

The tell is specific: a future-tense "I'll wait/continue once X" as the
final message, with no tool call in that same turn that actually blocks or
arms a wakeup.

Seen once, redispatch and let it correct itself. **Seen twice in a row for
the same wait condition, stop redispatching the same vague prompt** — be
explicit: tell it to block synchronously *within the same turn* and only
end that turn once it has a real result. Hand it the actual blocking
command (`run_monitor_sync`, `wait_run`, a CI-watch command) rather than
telling it to "wait" — "wait" is the instruction that produced the passive
wait in the first place.

### A success status does not mean the sub-work reported

In-process subagents (your CLI's own `Agent` tool, not platform runs) are
tied to the parent session's process: **when the parent's turn ends, they
die with it, unreported.** A dispatched review once spawned four of them,
narrated "I'll wait for their completion notifications," and ended its turn.
The parent still read `status=success` — the turn ended without an error —
so nothing looked wrong. Only `run_tree` showing **zero child runs** exposed
that no review had happened and no verdict was ever recorded.

**`run_status=success` + `run_tree` with zero children is the signature of
this failure, not evidence of a clean reviewed result.** If a session's own
narration says it dispatched sub-work and is waiting on it, confirm that
work exists as independent, trackable runs.

Diagnostic sequence for a delegate that looks stalled: is the card idle →
`run_status` / `run_tree` to confirm it is genuinely idle with nothing armed
rather than just slow → `run_agent_async` back into that **same
`session_id`** with an explicit corrective instruction ("you said you'd
wait, but nothing is armed — do X now").

### Arming a monitor is not proof it is watching

`run_monitor_async` can die seconds after being armed, with nothing
notifying anyone — the arming call's success being the only evidence a
watcher ever existed. If anything meaningful depends on that monitor
firing, check the monitor run's own `run_status` after a short delay.

Related: `schedule_wakeup` can flatly refuse a root/non-interactive session
with no delivery anchor ("no delivery channel and no delivery address could
be resolved for this session"). The mechanism that works for that class of
session is `call_me_back=true` on an already-dispatched `run_agent_async`,
not `schedule_wakeup` on itself. And when both the async monitor and the
wakeup are unavailable, blocking synchronously in-turn is the correct
fallback, not a workaround to feel bad about — ending the turn with no
terminal action is worse.

## The same completion can arrive several times

A chain's completion fires through independent channels, and each one can
deliver the same terminal event: the run's own callback, a flow-idle
notification, a stale `list_target_runs` / `supervisor_status` poll, an
internal tooling retry resending the event, and — a fifth mechanism —
**your own session's auto-compaction**, which surfaces exactly like a
dispatched agent's completion and echoes your own prior report back at you.
In one long orchestration this happened 15+ times for chains that had each
genuinely completed once.

This is not misreading a signal's meaning. It's the *same* correctly
terminal event arriving more than once.

On any supervisor/run/flow wakeup, before reporting to a human or taking
further action:

1. **Re-fetch durable state** — `get_kanban_card` and/or `run_status`.
   Don't trust the wakeup payload's framing on its own.
2. **Compare against what you last reported** for that card/run. Same
   terminal status, same comments, nothing changed → it's an echo. Suppress
   it. Don't re-notify, and above all don't re-dispatch a chain that
   already finished.
3. One `get_kanban_card` call is cheaper than a duplicate "it's done!"
   message, and far cheaper than re-triggering completed work.

**Fast way to recognise the compaction echo** before even reading its
content: `get_run` or `run_tree` on the triggering run. `initiator_kind:
auto_compact` with `input: "/compact"` and a `parent_run_id` pointing at
your own session is internal housekeeping, not new work, whatever the
echoed payload claims.
