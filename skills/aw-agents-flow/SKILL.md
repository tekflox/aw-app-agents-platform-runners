---
name: aw-agents-flow
description: Agents Flow — a drag-and-drop graph in Agents Platform defining agent-to-agent handoffs, starting from a Source node (inbound channel). Not an execution engine, not tied to Notion. Auto-injected into an agent's system prompt when it's a node in an ENABLED flow. Use for Agents Flow, handoff, or editing a flow at /agents-flow.
---

# aw-agents-flow — agent-to-agent handoff

You're running as part of an **enabled Agents Flow**: you must end this turn
with one of 3 explicit actions — hand off, report back, or declare the task
done. Loaded automatically; nothing to do to "activate" it.

Right after this text, your dispatch also tells you: (1) which agents are
directly connected to you in the flow diagram — a starting point, not a
restriction (see below), and (2) whether someone's waiting for your result
(this run was dispatched with `call_me_back=true`), and if so, who.

## Run-ID precondition

Only use the terminal actions in this skill when the current dispatch
provides an explicit Agents Platform run ID. If no run ID is available,
treat the session as an ordinary continuing conversation: do not call a
terminal-action tool, and do not search for, infer, or fabricate a run ID
just to call one. No terminal action is required in that case.

## Loose by design

**No enforcement on WHO you call** — any agent on the platform, not just
the connected list (`list_agents` for everyone else). **What IS enforced:**
ending the turn with one of the 3 actions below — a "never leave the flow
hanging" guarantee, not a permission system.

`return_to_caller_agent`'s `kind` and `mark_flow_done`'s `outcome` are
required structured fields — enums the platform validates, so your decision
is checkable from the tool call itself, not parsed out of prose.
`message`/`summary` stay free text for the human-readable detail.

## The 3 terminal actions

### ① `run_agent_async` — hand off, no context carried

Dispatches a **fresh** run of another agent — it sees none of your own
conversation, only the `input` text you give it. Use when you're done with
your part and want a different agent (or fresh instance) to continue.

```
run_agent_async(slug="<agent-slug>", input="<what they need to know>",
                target_slug="<same target you're running under>",
                call_me_back=false)
```

`call_me_back=false` for a clean handoff; `true` only if you want to be
woken up once they're done. `notion_task_id` is optional — omitted, the new
run auto-inherits this run's own card; pass it explicitly only to target a
*different* card.

**Redirect the wake-up to someone else (`call_me_back_on`, 2026-07-17):**
by default a `call_me_back=true` dispatch wakes YOUR OWN session. Pass
`call_me_back_on="<session_id>"` to wake a **different** session instead
once the dispatched run finishes — chains a hand-off in one call ("run
agent B, and when B is done, wake up session C") instead of B having to
explicitly call C itself with `return_to_caller_agent` or another
`run_agent_async`. Ignored when `call_me_back=false`.

**Changed your mind after dispatching with `call_me_back=false`
(`register_callback`, 2026-07-17):** if you fire-and-forgot a run and later
want to be woken up when it finishes, `register_callback(run_id="<the run "
"you dispatched>")` subscribes retroactively — same delivery mechanism as
`call_me_back=true`, just armed after the fact instead of at dispatch time.
Optional `session_id` redirects the wake-up to a different session (same as
`call_me_back_on`). If the run already finished by the time you call this,
the wake-up fires immediately with its result instead of being dropped.

### ② `return_to_caller_agent` — reuse context, talk to a specific caller

Resumes the **exact session** of whoever called YOU via `run_agent_async`,
full context intact. Use to answer something the caller would recognize as
a continuation, not a cold restart.

```
return_to_caller_agent(message="<what you want to tell them>", kind="result")
```

`kind` (required): `result` (you finished, this is the outcome), `question`
(caller must decide something before you continue), `blocker` (you're stuck,
need help). Fails cleanly if you have no caller (you're the chain root).
No-ops safely (`{ok:true, noop:true}`) if this run was itself dispatched with
`call_me_back=true` — the caller gets your result automatically, calling
this too is redundant but harmless.

### ③ `mark_flow_done` — declare the task finished

```
mark_flow_done(summary="<what you did>", outcome="success")
```

`outcome` (required) drives Kanban status: `success`/`partial` → `done`;
`failed` → `need_human` (summary becomes the required explanation). If this
run carries a card, this moves it for you — no separate `move_kanban_task`
needed.

**QA accountability (enforced server-side):** pass `qa_run_id` (the QA run's
`Run.id`) or `qa_not_needed=true`. Neither given → backend auto-looks-up a
recent succeeded `qa-*` run against the same card/target; only rejects if
none is found. Both given, or a `qa_run_id` that doesn't resolve → rejected.

**Flow-level wakeup (2026-07-16):** whichever run first registered a
`call_me_back` anywhere in this flow's chain (`core.wakeups.FlowWaiter`,
keyed by `flow_run_id`) gets resumed once `mark_flow_done`/`mark_as_planned`
fires, however many hops deep that happened — separate from, and in addition
to, the ordinary one-hop `call_me_back` wakeup each dispatch already gets.

### ③b `mark_as_planned` — declare PLANNING (not implementation) concluded

```
mark_as_planned(summary="<what was planned>")
```

Use instead of `mark_flow_done` when your output is a plan/design/spec (e.g.
Architect), not a shippable implementation — moves the card to `planned`
instead of `done` so a finished design isn't confused with a finished
feature. Counts as a valid terminal action; no QA accountability needed.
Rule of thumb: still needs a human/coder to build it → `mark_as_planned`;
you also built and verified it → `mark_flow_done`.

## Stuck and need a human — `ask_human`

Not a 4th terminal action — call mid-turn, then still end with one of the 3
above (usually `mark_flow_done(outcome="failed", ...)` or a handoff).

```
ask_human(question="<the exact decision/info you need, in plain language>")
```

Use once you've actually investigated and still can't confidently proceed —
a genuine product/business call or a fact truly not discoverable in
code/history. Sends the question via the sysadmin Telegram bot as a
clickable link; your session auto-resumes with their answer as the next
prompt. Works with or without a Kanban card — **if there is one**, also call
`move_kanban_task(status="need_human", comment=...)` yourself; `ask_human`
doesn't touch Kanban.

## When the terminal action itself FAILS to execute

A tool call that errors or times out is **not** the same as work that
failed, and the two must not land in the same place. `mark_flow_done(
outcome="failed")` says *the task did not work out*. A `mark_flow_done`
that raises a connection error says *I could not record what already
happened* — the verdict you reached is still valid, it just has not been
written down.

**Do not touch the card's status to report a failed terminal action.** In
particular do not call `set_blocker` or `move_kanban_task(status=
"need_human")` for it. Doing so overwrites a healthy delivery with a status
that reads as "this needs a human decision", and the next person to see the
board has no way to tell that the review actually passed. Any verdict you
already recorded — `set_qa_status`, a comment, a property — stands; leave it
alone.

**Do this instead:**

1. **Retry once.** These failures are usually a gateway upstream that
   dropped and is re-checked every 60s (`aw-workspace-cli doctor` names it:
   "an upstream the gateway failed to connect to serves zero tools until a
   reload"). A second attempt often just works.
2. **If it still fails, end your turn** and say so in your final output, in
   plain words: which tool, what error, and what the outcome would have
   been. Name the run id.

Ending without a terminal action is already handled, and handled better
than anything you can improvise: the runtime **reprompts you once**, which
is a free second attempt after the upstream has had time to recover, and if
that also produces nothing it escalates — setting `flow_needs_human` on
every run in the flow (the yellow border on the Flow chip) and pinging
sysadmins on Telegram with your run id and the reason. See
`_maybe_reprompt_or_escalate` / `_escalate_need_human` in
`core/executor.py`.

So the honest failure path costs one reprompt and produces an alert that
carries the run id. Reaching for `set_blocker` instead produces a card that
lies about a delivery, and no alert at all.

**Live example (2026-08-21).** A QA agent finished a review, called
`set_qa_status` successfully — the `aw-kanban` upstream was fine throughout
— and then hit connection errors on `mark_flow_done` twice and a timeout on
`return_to_caller_agent`, both on the `agents-platform-runners` upstream.
Following its own "don't hunt for workarounds, flag it" rule, it called
`set_blocker`. That moved a card whose delivery had just passed review to
**Need Human**, and a human had to read the whole run to discover nothing
was wrong. The agent obeyed its contract exactly; the contract was missing
this section.

## What this is NOT

Not LangChain/LangGraph (no engine drives execution for you — you decide
each time). Not a permission system on who you may call. Not tied to Notion
Kanban (a flow can exist independently, though a card is a common `Source`).

## Gotchas for anyone editing the runtime (core/executor.py, core/wakeups.py)

- **`model_slug` backfill:** `_run_agent_impl` only backfills `model_slug`
  onto a Run row when it creates that row itself. `ask_human` resume and the
  native flow reprompt both pre-create the row (explicit `run_id`, to avoid
  a race) before calling `run_agent()`, landing in the "row already exists"
  branch that skips it — any new pre-create-then-run path needs to copy
  `model_slug` explicitly too, and should be covered by
  `rearm_stuck_wakeup_runs()`'s boot-time sweep (re-fires runs stuck
  `pending`/`initiator_kind=wakeup` for 20s+).
- **Kanban-move vs. an active QA cycle:** `mark_flow_done` and
  `_escalate_need_human` both move the card via `/api/notion/kanban/move`,
  which hard-locks `done`/`need_human`/`ready_to_deploy` while
  `QAStatus=In Progress`. Neither falls back to `set-qa-status` to force it
  through (that would clobber an active QA review) — a rejected/failed move
  just pings sysadmins on Telegram so a human notices, rather than dropping
  it silently.
- An agent in multiple enabled flows gets a merged connected-agents list —
  fine as-is, revisit only if it gets noisy in practice.
