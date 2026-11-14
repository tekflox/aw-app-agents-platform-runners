"""``work_loop`` — deterministic orchestrator retiring the two-tool split
between ``kanban_check_deployed`` (this app) and
``list_kanban_cards_need_attention`` (aw-app-notion).

Architect decision, Kanban card 4165bf3b-9510-810d-b54d-f90efc70e3bc — THIN:
no LLM call anywhere in this module. Every loop here either runs a fully
deterministic check itself (the ``deploy`` loop, unchanged, see
``deploy_reconcile.check_deployed``) or computes signals/candidates and
packages anything needing real judgment into a Mode B work-order list
(``{page_id, reason, evidence, suggested_action}`` — byte-compatible with
the contract already documented in aw-app-maintenance-agents'
``aw-manager`` SKILL.md) for the Manager agent to actually route. This
module never dispatches the Manager itself — that hook (``dispatch_manager``)
lives in ``mcp_server.py``'s tool handler, the same split as every other
platform dispatch in this app (``sweep_ready`` leaves its own dispatch to
the caller's ``PlatformClient``; QA dispatch for ``kanban_check_deployed``
lives in the handler, not in ``deploy_reconcile.py``).

Phase 1 of a 3-PR migration (card 4165bf3b-9510-81bd): ``deploy``,
``attention`` and ``ready`` are wired. ``plans`` is a reserved enum
value — requesting it comes back in ``loops_skipped`` as
``"plans: not implemented"``, never an error, so a caller can opt in early
and the ``loops`` param stays forward-stable.

A ``prs`` loop (two-phase ``gh`` sweep + viewed-stamp back-off) was added in
card 4175bf3b-9510-8163 and then retired in card 4185bf3b-9510-81a7 —
superseded by aw-app-git's ``GET /api/apps/git/github/maintenance`` gate +
the AP-MT ``github-maintenance`` Plan (design: card 4185bf3b-9510-81fc,
section 3), which covers the same ground without this app's blind spot for
PRs on the root ``aw-workspace`` checkout itself. The two-phase sweep/
backoff logic lives on in aw-app-git's ``github_maintenance`` module, ported
rather than imported.

Loop interface: each loop is ``async (ctx) -> (section, work_orders,
actions)`` — adding a loop later is one new function + one registry entry,
nothing else here changes. ``ctx`` (``WorkLoopContext``) carries everything
a loop needs (board client, an injected AP-MT ``list_runs`` probe, now/
dry_run/limits) so no loop function reaches for a module-level global.

Per-loop AND per-card error isolation (card instructions item #6, mirroring
``deploy_reconcile.check_deployed``'s own per-card ``BoardUnavailable``
guard, PR #25): a dead loop is caught by ``run_work_loop`` and reported in
``errors`` — every OTHER requested loop still runs. A dead card inside the
``attention`` loop's run-check is caught per-candidate the same way; the
``deploy`` loop's own per-card guard already lives in ``check_deployed``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Awaitable, Callable

from . import deploy_reconcile
from . import kanban_dispatch

log = logging.getLogger("aw_apps.agents_platform_runners.work_loop")

DEFAULT_LOOPS = ("deploy", "attention")
NOT_IMPLEMENTED_LOOPS = ("plans",)

DEFAULT_ATTENTION_LIMIT = 10
MIN_ATTENTION_LIMIT = 1
MAX_ATTENTION_LIMIT = 25

# aw-manager SKILL.md's own Batch 1 gate ("Batch 1 — stale In Progress"),
# absorbed verbatim per design §3.D — not re-derived.
STALE_RUN_MINUTES = 30

# "Respect the 25-probe spirit" (design §9, "Risks for the coders") — caps
# how many candidates reach the run-check (post back-off) in one pass.
MAX_RUN_CHECK_PROBES = 25


ListRunsFn = Callable[..., Awaitable["tuple[list[dict] | None, str | None]"]]
"""``async (*, notion_task_id=None, category=None, limit=20) -> (rows, error)``.
Never raises — a probe failure comes back as ``(None, "...")`` so a loop can
fail CLOSED on that one candidate (``agent_activity: unknown``) instead of
taking down the whole pass."""


@dataclasses.dataclass
class WorkLoopContext:
    """Everything a loop function needs, so none of them reach for a
    module-level global or own an httpx client directly — ``mcp_server.py``
    builds this once per call and owns every platform credential inside
    ``list_runs``."""

    board: "kanban_dispatch.BoardClient"
    list_runs: ListRunsFn
    now: float
    dry_run: bool = False
    page_id: str | None = None
    limit: int = DEFAULT_ATTENTION_LIMIT
    max_cards: int = deploy_reconcile.DEFAULT_MAX_CARDS
    reping: bool = True
    platform: "kanban_dispatch.PlatformClient | None" = None


# ── deploy loop ──────────────────────────────────────────────────────────

async def _deploy_loop(ctx: WorkLoopContext) -> "tuple[dict, list[dict], list[dict]]":
    """Thin wrapper over the UNCHANGED ``check_deployed`` (card instructions
    item #2) — every verdict here is already fully deterministic end-to-end,
    so this loop never contributes a work order, only ``actions_taken`` for
    whatever it actually moved. The QA-dispatch/reminder platform glue stays
    in ``mcp_server.py`` exactly as it does for the ``kanban_check_deployed``
    alias — a platform concern, same split as ``sweep_ready`` leaving
    dispatch to its caller's ``PlatformClient``."""
    result = await asyncio.to_thread(
        deploy_reconcile.check_deployed, ctx.board,
        page_id=ctx.page_id, dry_run=ctx.dry_run, max_cards=ctx.max_cards)
    actions = [{"loop": "deploy", "page_id": row["page_id"], "action": row["action"],
                "detail": f"verdict={row.get('verdict')}"}
               for row in result.get("moved", [])]
    return result, [], actions


# ── attention loop ───────────────────────────────────────────────────────

_SINGLE_CARD_PROPS = ["Name", "Status", "LastSweptAt", "LastHumanPingAt", "HumanPingCount"]

# The raw Notion option labels the attention lane cares about — same two
# values ``need_attention``'s own board-wide scan filters to (aw-app-notion's
# ``list_cards_need_attention``), and the same literals ``check_deployed``
# and ``kanban_dispatch.move`` already compare against elsewhere in this app
# ("Need Human" at deploy_reconcile.py:812/825, "In Progress" per aw-manager
# SKILL.md's own "raw Notion option label for Batch 1's running filter").
_ATTENTION_ELIGIBLE_STATUSES = ("In Progress", "Need Human")


def _single_card_attention_source(board: "kanban_dispatch.BoardClient", page_id: str) -> dict:
    """``page_id`` single-card mode (design §2, same semantics as
    ``kanban_check_deployed``'s own param) — bypasses the board-wide
    ``need_attention`` scan for the one card named, but still gates on the
    SAME status filter that scan applies (code-review finding, PR #36: a
    page_id naming a Done/Backlog card used to still run the full backoff +
    agent_activity pipeline and could emit a spurious work order recommending
    resumption for a card that isn't stuck at all). Shaped exactly like
    ``need_attention``'s own return either way, so the rest of the loop below
    doesn't need to know which source it came from. Reping is meaningless for
    a single named card (it's a board-wide batched mechanism) — this source
    never reports it, by design, not by omission."""
    props = board.properties(page_id, _SINGLE_CARD_PROPS)
    status = props.get("Status") or ""
    if status not in _ATTENTION_ELIGIBLE_STATUSES:
        return {"has_work": False, "cards": [],
                "counts": {"count": 0, "scanned": 1, "truncated": False},
                "skipped_reason": f"status={status!r} is not In Progress or Need Human"}
    card = {"page_id": page_id, "title": props.get("Name") or "", "status": status,
            "last_swept_at": props.get("LastSweptAt"),
            "last_human_ping_at": props.get("LastHumanPingAt"),
            "human_ping_count": props.get("HumanPingCount")}
    return {"has_work": True, "cards": [card], "counts": {"count": 1, "scanned": 1,
                                                           "truncated": False}}


def _latest_comment_at(comments_md: str | None) -> float | None:
    """Epoch seconds of the LAST comment line, or ``None`` if there are no
    comments or the timestamp doesn't parse. Mirrors aw-manager SKILL.md's
    "Back-off — LastSweptAt": ``comments_md`` lines read
    ``"- **<created_time>** — text"``, oldest -> newest; only the newest one
    matters for the back-off re-arm check."""
    last_ts: str | None = None
    for line in (comments_md or "").splitlines():
        line = line.strip()
        if line.startswith("- **"):
            rest = line[4:]
            end = rest.find("**")
            if end != -1:
                last_ts = rest[:end]
    return deploy_reconcile._parse_iso_datetime(last_ts) if last_ts else None


def _backoff_state(last_swept_at: str | None, comments_md: str | None) -> str:
    """``"eligible"`` | ``"backed_off"`` — aw-manager SKILL.md's back-off
    rule, read-only here (the write stays with whichever actor actually
    touches the card, design §3.C). Fails OPEN at every step (unset/
    unparseable stamp, unreadable comments, unparseable comment timestamp)
    — "one wasted touch beats a card permanently stranded by a read that
    can't be trusted" (same skill, "Fail open")."""
    if not last_swept_at:
        return "eligible"
    swept = deploy_reconcile._parse_iso_datetime(last_swept_at)
    if swept is None:
        return "eligible"
    if comments_md is None:
        return "eligible"
    latest = _latest_comment_at(comments_md)
    if latest is None:
        return "eligible"
    return "eligible" if latest > swept else "backed_off"


async def _agent_activity(page_id: str, list_runs: ListRunsFn, now: float) -> dict:
    """aw-manager SKILL.md's "Batch 1 — stale In Progress" / "Run check
    (non-negotiable exclusion)", absorbed verbatim per design §3.D — the
    two-call shape (an unfiltered liveness probe, then the pinned
    ``category="work"`` lookup) and the qa-* exclusion are not re-derived,
    this app already fronts the same AP-MT endpoint via its own
    ``list_runs`` tool.

    Returns ``{"state": ...}`` where state is one of:
    ``live`` (something non-terminal is in flight — never touch),
    ``recent`` (terminal, but inside the 30-min window — wait it out),
    ``stale`` (terminal, outside the window, a resumable work run exists),
    ``none`` (no run at all, or no non-monitor run ever dispatched),
    ``unknown`` (a probe itself failed — fail CLOSED, never qualifies).
    """
    rows, error = await list_runs(notion_task_id=page_id, limit=1)
    if error is not None:
        return {"state": "unknown", "error": error}
    if not rows:
        return {"state": "none"}

    newest = rows[0]
    status = (newest.get("status") or "").lower()
    if status in ("running", "pending"):
        return {"state": "live", "live_run_id": newest.get("id"),
                "source_slug": newest.get("source_slug")}

    last_activity = newest.get("ended_at") or newest.get("updated_at") or newest.get("created_at")
    last_at = deploy_reconcile._parse_iso_datetime(last_activity)
    if last_at is not None:
        minutes = (now - last_at) / 60.0
        if minutes <= STALE_RUN_MINUTES:
            return {"state": "recent", "last_ended_at": last_activity,
                    "minutes_since_activity": round(minutes, 1)}

    # Terminal and outside the window (or an unparseable timestamp — fail
    # OPEN into the pinned lookup rather than silently skip; staleness, not
    # freshness, is what's at stake here, the opposite direction from
    # deploy_reconcile's own "never mistake stale for fresh" rule).
    work_rows, work_error = await list_runs(notion_task_id=page_id, category="work", limit=10)
    if work_error is not None:
        return {"state": "unknown", "error": work_error}
    if not work_rows:
        return {"state": "none"}

    non_qa = [r for r in work_rows if not (r.get("source_slug") or "").startswith("qa-")]
    # "On a card with >10 sub-dispatches since the real session, resume_run_id
    # is wrong-by-construction; report resume_window_exceeded:true rather than
    # widening the limit" (design §9) — the only case this app can actually
    # detect from inside the 10-row window is "every row we can see is
    # qa-*", so that is what trips the flag; a real session further back
    # than 10 rows is invisible to it and simply not reported.
    resume_window_exceeded = not non_qa
    qualifying = non_qa[0] if non_qa else work_rows[0]

    q_status = (qualifying.get("status") or "").lower()
    if q_status in ("running", "pending"):
        return {"state": "live", "live_run_id": qualifying.get("id"),
                "source_slug": qualifying.get("source_slug")}

    q_last = qualifying.get("ended_at") or qualifying.get("updated_at") or qualifying.get("created_at")
    q_at = deploy_reconcile._parse_iso_datetime(q_last)
    out = {"state": "stale", "resume_run_id": qualifying.get("id"),
           "source_slug": qualifying.get("source_slug"), "last_ended_at": q_last}
    if q_at is not None:
        out["minutes_since_activity"] = round((now - q_at) / 60.0, 1)
    if resume_window_exceeded:
        out["resume_window_exceeded"] = True
    return out


async def _attention_loop(ctx: WorkLoopContext) -> "tuple[dict, list[dict], list[dict]]":
    if ctx.page_id:
        raw = await asyncio.to_thread(_single_card_attention_source, ctx.board, ctx.page_id)
    else:
        raw = await asyncio.to_thread(ctx.board.need_attention, limit=ctx.limit, reping=ctx.reping)

    candidates: list[dict] = []
    work_orders: list[dict] = []
    actions: list[dict] = []
    probes = 0

    for card in raw.get("cards", []):
        page_id = card.get("page_id") or ""
        last_swept_at = card.get("last_swept_at")
        comments_md = None
        if last_swept_at:
            try:
                fetched = await asyncio.to_thread(ctx.board.card, page_id, with_content=True)
                comments_md = fetched.get("comments_md")
            except kanban_dispatch.BoardUnavailable:
                comments_md = None  # fail open — see _backoff_state
        backoff = _backoff_state(last_swept_at, comments_md)

        entry = {**card, "backoff": backoff}
        if backoff == "backed_off":
            entry["agent_activity"] = {"state": "unknown",
                                       "reason": "backed off — skipped before the run-check"}
            entry["qualifies_for_dispatch"] = False
            candidates.append(entry)
            continue

        if probes >= MAX_RUN_CHECK_PROBES:
            entry["agent_activity"] = {"state": "unknown", "reason": "run-check probe cap reached"}
            entry["qualifies_for_dispatch"] = False
            candidates.append(entry)
            continue
        probes += 1

        activity = await _agent_activity(page_id, ctx.list_runs, ctx.now)
        entry["agent_activity"] = activity
        qualifies = activity.get("state") in ("stale", "none")
        entry["qualifies_for_dispatch"] = qualifies
        candidates.append(entry)

        if qualifies:
            if activity.get("state") == "stale":
                evidence = (f"newest work run {activity.get('resume_run_id')} "
                           f"(source={activity.get('source_slug')}) ended "
                           f"{activity.get('minutes_since_activity')}min ago")
                suggested = f"resume via invoke_kanban_agent run_id={activity.get('resume_run_id')}"
            else:
                evidence = "no dispatched run found for this card"
                suggested = "needs routing — no prior run to resume"
            work_orders.append({"page_id": page_id,
                                "reason": f"{card.get('status')} card, no live agent activity",
                                "evidence": evidence, "suggested_action": suggested})

    for pinged in (raw.get("re_pinged") or []):
        actions.append({"loop": "attention", "page_id": pinged.get("page_id"),
                        "action": "re_pinged",
                        "detail": f"human_ping_count={pinged.get('human_ping_count')}"})

    section = {k: v for k, v in raw.items() if k != "cards"}
    section["candidates"] = candidates
    return section, work_orders, actions


# ── ready loop ───────────────────────────────────────────────────────────

async def _ready_loop(ctx: WorkLoopContext) -> "tuple[dict, list[dict], list[dict]]":
    """Thin wrap of the existing, fully-tested ``sweep_ready`` — no new
    dispatch logic, just plumbed in as another ``work_loop`` loop option
    per the design's tool shape (§2). Requires ``ctx.platform`` (an AP-MT
    dispatch client); without one this loop is not usable and is skipped
    with an explicit reason rather than guessing at a BASE URL of its own."""
    if ctx.platform is None:
        return ({"error": "ready loop requires a platform client — not wired by this caller"},
                [], [])
    result = await kanban_dispatch.sweep_ready(
        ctx.board, ctx.platform, status="ready", limit=ctx.limit, dry_run=ctx.dry_run)
    actions = [{"loop": "ready", "page_id": row["page_id"], "action": "dispatched",
               "detail": f"run_id={row.get('run_id')}"}
              for row in result.get("results", []) if row.get("run_id") and not row.get("skipped")]
    return result, [], actions


LOOP_FUNCS: "dict[str, Callable[[WorkLoopContext], Awaitable[tuple[dict, list[dict], list[dict]]]]]" = {
    "deploy": _deploy_loop,
    "attention": _attention_loop,
    "ready": _ready_loop,
}


async def run_work_loop(ctx: WorkLoopContext, loops: "list[str]") -> dict:
    """The tool contract, minus ``boards``/``manager_dispatch`` — those need
    the platform dispatch client and the (today single, hardcoded)
    multi-board config, which stay owned by ``mcp_server.py`` the same way
    ``check_deployed`` already leaves platform dispatch to its caller.

    ``dry_run`` totality (card instructions item #7) is enforced HERE, once,
    rather than trusting every loop to remember it: ``reping`` is forced off
    whenever ``dry_run`` is set, since a reping write is the one side effect
    the attention loop could otherwise take even in a "preview" call. Every
    other side effect (deploy's moves/comments/stamps, ready's claim/move/
    dispatch) is already dry_run-total inside the functions this wraps.

    Requested loops run CONCURRENTLY (``asyncio.gather``), not sequentially —
    the three loops are already independent (none reads another's output;
    none mutates ``ctx``), so the old sequential ``for`` loop paid the FULL
    SUM of every loop's latency as this call's own critical path (deploy's
    Notion scan + attention's board-wide scan plus up to
    ``MAX_RUN_CHECK_PROBES`` per-candidate AP-MT run-checks + ready's sweep,
    one after another) instead of the max of them. That sum is exactly what
    pushed the documented default ``["deploy","attention"]`` — and the
    ``["deploy","attention","ready"]`` combination reported in
    bug:work-loop-multi-loop-timeout — past the caller's tool-call timeout,
    even though each loop alone stays comfortably inside it.

    Thread-safety of the shared ``ctx.board`` (``kanban_dispatch.BoardClient``)
    across these concurrent ``asyncio.to_thread`` calls (code-review finding,
    PR #40): verified safe, not assumed. ``BoardClient`` holds exactly one
    instance attribute, ``self._base``, set once in ``__init__`` and never
    reassigned; every method routes through ``_request``, which opens a brand
    new ``urllib.request.Request``/``urlopen`` per call — no shared session,
    connection pool, or retry/rate-limit state lives on the instance for two
    concurrent calls to corrupt. Two threads calling it at once are two
    independent HTTP requests, exactly as if two different ``BoardClient``
    instances had made them.
    """
    if ctx.dry_run and ctx.reping:
        ctx = dataclasses.replace(ctx, reping=False)

    work_orders: list[dict] = []
    actions_taken: list[dict] = []
    errors: list[dict] = []
    loops_skipped: list[str] = []
    sections: dict[str, Any] = {}

    runnable: list[str] = []
    for loop_name in loops:
        if loop_name in NOT_IMPLEMENTED_LOOPS:
            loops_skipped.append(f"{loop_name}: not implemented")
            continue
        if loop_name not in LOOP_FUNCS:
            loops_skipped.append(f"{loop_name}: unknown loop")
            continue
        runnable.append(loop_name)

    async def _run_one(loop_name: str):
        try:
            return await LOOP_FUNCS[loop_name](ctx)
        except kanban_dispatch.BoardUnavailable as exc:
            return exc
        except Exception as exc:  # pragma: no cover - defensive, per loop
            log.warning("work_loop: loop %s failed", loop_name, exc_info=True)
            return exc

    # return_exceptions=False here would be wrong twice over: it would let
    # one dead loop cancel every other still-in-flight loop (the very
    # isolation guarantee this function exists to provide), and gather would
    # raise instead of letting us report the failure in `errors` below — so
    # every per-loop exception is caught INSIDE `_run_one` and handed back
    # as a value, never raised out of gather.
    outcomes = await asyncio.gather(*(_run_one(name) for name in runnable))

    attempted = len(runnable)
    for loop_name, outcome in zip(runnable, outcomes):
        if isinstance(outcome, Exception):
            errors.append({"loop": loop_name, "error": str(outcome)})
            continue
        section, orders, actions = outcome
        sections[loop_name] = section
        work_orders.extend(orders)
        actions_taken.extend(actions)

    # Code-review finding (PR #36, round 1 — left unfixed round 2): hardcoding
    # ok:True regardless of `errors` made mcp_server.py's `except
    # BoardUnavailable: return _err(503, ...)` guard around this call dead
    # code — every loop's own BoardUnavailable is already caught right above,
    # so it never propagates out for that guard to catch. `ok` is False only
    # when EVERY loop actually attempted failed (a total outage reported
    # honestly), never for a partial failure — one bad loop among several
    # successful ones is still a usable report, same as check_deployed's own
    # per-card errors not flipping its own `ok` to False.
    ok = not (attempted > 0 and len(errors) == attempted)

    return {
        "ok": ok,
        "dry_run": ctx.dry_run,
        **sections,
        "work_orders": work_orders,
        "actions_taken": actions_taken,
        "loops_skipped": loops_skipped,
        "errors": errors,
    }


__all__ = ["WorkLoopContext", "run_work_loop", "DEFAULT_LOOPS",
           "NOT_IMPLEMENTED_LOOPS", "STALE_RUN_MINUTES", "MAX_RUN_CHECK_PROBES",
           "DEFAULT_ATTENTION_LIMIT", "MIN_ATTENTION_LIMIT", "MAX_ATTENTION_LIMIT"]
