---
name: aw-devteam-dispatch-tracker
description: How a Source session (Telegram, Watch, or any other inbound channel) opens a Kanban card for a dev-team ask, dispatches it into the Dev Team flow, and drives it to completion without polling. Distinct from aw-kanban-sweeper, which sweeps STALE already-open cards days later — this is the live create-dispatch-watch-close loop used the moment a request comes in. Use whenever acting as Source for a build/fix/investigate/design request, or when documenting/reviewing that pattern.
---

# aw-devteam-dispatch-tracker — create, dispatch, watch, close

This is the loop a Source session (today: `aw-agent-telegram`) runs every
time a human asks for something that belongs in the codebase rather than
in chat — "add X", "fix Y", "plan Z". It is NOT the kanban-sweeper's job
(that one resumes stale cards days later to ask "is this actually done?").
This is the live version, run the moment the request lands.

## The loop

1. **Search the knowledge base first.** Query the ask itself before
   picking an agent or asking Frederico anything — the answer to "have we
   tried this" is often already written down.

2. **Create (or reuse) a Target.** `list_targets(q=...)` first; only
   `create_target(slug=..., name=..., description=...)` if nothing fits.
   Every run dispatched for this ask carries this `target_slug`.

3. **Create the Kanban card.**
   ```
   create_kanban_task(title="...", description="...", input_text="<the ask, quoted in full>",
                       target_slug="...", agent_slug="<entry agent>")
   ```
   Card lands in Backlog. Move it to `running` once dispatched — moving to
   `ready` fires nothing by itself (dispatch is this app's job, not
   aw-kanban's).

4. **Pick the entry agent and dispatch directly** — never rely on a status
   move to trigger anything:

   | Signal | Dispatch to |
   |---|---|
   | Vague ask, scope needs shaping | `product-owner` |
   | Scope is clear, it's a design/approach question | `architect` |
   | "Why is X broken" | `debugger` |
   | Small, well-scoped fix, no design decision | `coder-sonnet` |
   | "Review this diff/PR/branch" | `code-reviewer-sonnet` |
   | Docs only | `doc-writer` |

   ```
   run_agent_async(slug="<agent>", input="<the ask in full>",
                    target_slug="...", notion_task_id="<card page_id>")
   ```

   `call_me_back` defaults to `true` — the dispatching Source session is
   woken automatically once the dispatched run ends its own turn.

5. **If the ask is design-only ("just plan it, don't build it"),** say so
   explicitly in the dispatch input and tell the agent to end with
   `mark_as_planned` (not `mark_flow_done`) — that moves the card to
   `planned` instead of `done`, so a finished design isn't mistaken for a
   finished feature.

6. **Watch the whole chain, not just the one dispatched run.** A multi-hop
   card (PO → Architect → Coder → QA) outlasts a one-shot's 60s
   auto-give-up window. Arm:
   ```
   supervise(session_id="<session_id from run_agent_async>", forever=true)
   ```
   `forever=true` does not disarm itself — step 7 is what closes the loop.

   If the entry run is itself inside an enabled **Agents Flow**
   (`flow_run_id`/`flow_slug` set on the run), the flow's own
   `call_me_back`-anywhere-in-the-chain wakeup already covers the whole
   chain once `mark_flow_done`/`mark_as_planned` fires — a redundant
   `supervise()` call is harmless but not required in that case. Check
   `get_run_detail`'s `flow_run_id` before assuming you need to arm one.

7. **When woken, read the card and act — don't stop at "I kicked it
   off."**
   ```
   get_kanban_card(page_id="...", include_body=true, include_comments=true)
   ```
   - `done` / `ready_to_deploy` / `planned` → `stop_supervisor(notion_task_id=...)`
     (select by `notion_task_id`, not `target_session_id` — a re-dispatched
     card accumulates several session ids), then report to the human with
     the evidence from the card's comments.
   - `need_human` → read why. A real product/scope question → ask the
     human (check the KB first). QA bouncing on a design question → route
     to Architect, not another coder guess. Scope question → Product Owner.
   - QA rejected the delivery → re-dispatch a coder with QA's comment as
     input. Same `coder-sonnet` by default; escalate to `coder-opus` on a
     repeat rejection with the same root cause, by judgement, not a count.

8. **Verify every "done"/"all green" self-report independently** before
   reporting it onward — `gh pr view`/`gh run list --json ...,createdAt`
   (read chronologically, not `gh pr checks`' per-check-name rollup) /
   `get_kanban_card` re-read. A dispatched agent's own claim of success is
   a claim, not a verified fact — this has caught real false-greens before
   (a cancelled CI run superseded by nothing, reported as "all pass").

## What this is not

Not a replacement for `aw-kanban-sweeper` (that one targets cards that
have gone stale — In Progress/Need Human for days — by resuming the
original session and asking if it's actually done, not by redoing the
work). Not a replacement for the full `aw-agents` skill (no plan
presentation, no approval gate — this is the lighter dispatch-and-go path
for a normal delivery, still surfacing a genuine decision rather than
silently picking for the human).
