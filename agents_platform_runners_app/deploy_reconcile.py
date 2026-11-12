"""``kanban_check_deployed`` — deterministic Ready-to-Deploy reconciliation.

Architect decision, Kanban card 4165bf3b-9510-8162-89d0-dca46ba66481. The
short version: a card in ``Ready to Deploy``/``Testing`` names exactly one
component (``DeployComponent``) and one target version (``DeployToVersion``),
optionally a list of workspaces (``DeployWorkspaces``, empty = this
workspace). This module reads those fields, asks the LIVE component what
version it is actually running (never CI green, never the marketplace
catalog, never a git tag), and moves the card only on a verdict TRANSITION —
re-running it N times is N-1 no-ops.

Split the same way ``kanban_dispatch.py`` splits board vs. platform: this
module owns the board (aw-app-notion) and the live observation adapters
(aw-workspace core + aw-backend). It deliberately does NOT dispatch a QA
agent itself — that is an agents-platform call, which belongs in
``mcp_server.py`` the same way ``sweep_ready`` leaves platform dispatch to
its caller's ``PlatformClient``. ``check_deployed()`` reports
``action="moved_testing"`` rows; the caller dispatches QA for those.

**Known gaps, by component class** (say so rather than guess — the
architect's own fail-closed rule): ``service:<name>`` has no live
observation adapter wired yet (would need SSH/docker-exec per the
``aw-autoskill-verify-deploy-in-container`` skill, not available to this
app's runtime) — it always resolves to ``unknown``. Cross-tenant
``DeployWorkspaces`` entries (naming a workspace other than this one) are
also always ``unknown`` — every credential this app holds
(``AW_WORKSPACE_API_KEY``, ``AGENTS_PLATFORM_TOKEN``) is scoped to exactly
one workspace membership.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from . import kanban_dispatch

log = logging.getLogger("aw_apps.agents_platform_runners.deploy_reconcile")

DEPLOY_STATUSES = ("Ready to Deploy", "Testing")
UNKNOWN_STREAK_LIMIT = 3
DEFAULT_MAX_CARDS = 25

DEPLOY_PROPS = [
    "Name", "Status", "TargetSlug", "QAStatus",
    "DeployEnv", "DeployComponent", "DeployWorkspaces", "DeployToVersion",
    "DeployFromVersion", "DeployCheckResult",
]

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

_BACKEND_URL_VAR = "AW_BACKEND_URL"
_WORKSPACE_VAR = "AW_WORKSPACE"
_TOKEN_VAR = "AGENTS_PLATFORM_TOKEN"


class ObservationError(RuntimeError):
    """A live adapter could not reach its source — always surfaces as the
    card's verdict going to ``unknown``, never fabricated as pending/deployed."""


# ── version identity ─────────────────────────────────────────────────────

def parse_semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    m = _SEMVER_RE.match(value.strip())
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def looks_like_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(value.strip()))


def parse_component(value: str) -> tuple[str | None, str]:
    """``(class, name)`` — class is one of app/service/core/remote-host, or
    ``None`` for an unrecognised shape. ``name`` is '' for core/remote-host."""
    text = (value or "").strip()
    if text in ("core", "remote-host"):
        return text, ""
    for prefix in ("app:", "service:"):
        if text.startswith(prefix):
            return prefix[:-1], text[len(prefix):]
    return None, text


def compare(klass: str, observed: str | None, to_version: str) -> str:
    """"deployed" | "pending" | "unknown" for one (observed, target) pair."""
    if not observed:
        return "unknown"
    if klass == "service":
        if not (looks_like_sha(observed) and looks_like_sha(to_version)):
            return "unknown"
        # Exact match only — merge-base ancestry needs a live checkout this
        # app's runtime doesn't have (see module docstring); a non-equal SHA
        # could be ahead OR diverged and we can't tell which, so "unknown"
        # rather than guessing "pending".
        return "deployed" if observed.lower() == to_version.lower() else "unknown"
    observed_v, target_v = parse_semver(observed), parse_semver(to_version)
    if observed_v is None or target_v is None:
        return "unknown"
    return "deployed" if observed_v >= target_v else "pending"


# ── live observation adapters ────────────────────────────────────────────

def _this_workspace() -> str:
    return os.environ.get(_WORKSPACE_VAR, "").strip() or "aw"


def _backend_base() -> str:
    return os.environ.get(_BACKEND_URL_VAR, "").strip().rstrip("/")


def _auth_headers() -> dict:
    token = os.environ.get(_TOKEN_VAR, "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _http_get_json(url: str, headers: dict, *, timeout: float = 20.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
        except ValueError:
            body = {}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise ObservationError(f"could not reach {url}: {exc.reason}") from None


def _cross_tenant_unknown(ws: str, credential: str) -> dict:
    return {"workspace": ws, "observed": None,
            "error": f"cross-tenant observation not wired — this app's {credential} is "
                     f"scoped to workspace {_this_workspace()!r} only"}


def observe_app(name: str, workspace: str) -> dict:
    """Live ``installed_version`` for app ``name``, from aw-workspace core's
    own ``GET /api/apps/{slug}/versions`` — never the marketplace catalog."""
    ws = workspace or _this_workspace()
    if ws != _this_workspace():
        return _cross_tenant_unknown(ws, "AW_WORKSPACE_API_KEY")
    base = kanban_dispatch.board_base_url()
    key = (os.environ.get(kanban_dispatch.API_KEY_VAR)
           or kanban_dispatch._from_env_file(kanban_dispatch.API_KEY_VAR))
    if not key:
        return {"workspace": ws, "observed": None,
                "error": f"{kanban_dispatch.API_KEY_VAR} is not set for this MCP upstream"}
    try:
        status, body = _http_get_json(f"{base}/api/apps/{name}/versions", {"X-Api-Key": key})
    except ObservationError as exc:
        return {"workspace": ws, "observed": None, "error": str(exc)}
    if status == 404:
        return {"workspace": ws, "observed": None,
                "error": f"app {name!r} is not installed on workspace {ws!r} (404)"}
    if status != 200:
        return {"workspace": ws, "observed": None,
                "error": f"GET /api/apps/{name}/versions returned {status}"}
    return {"workspace": ws, "observed": (body or {}).get("installed_version"), "error": None}


def observe_core(_name: str, workspace: str) -> dict:
    """Live workspace/core version via aw-backend's own
    ``GET /api/workspaces/{slug}/versions`` — the same seam
    ``test_current_newer_than_latest_is_not_outdated`` guards."""
    ws = workspace or _this_workspace()
    if ws != _this_workspace():
        return _cross_tenant_unknown(ws, "AGENTS_PLATFORM_TOKEN")
    base = _backend_base()
    if not base:
        return {"workspace": ws, "observed": None,
                "error": f"{_BACKEND_URL_VAR} is not set for this MCP upstream"}
    try:
        status, body = _http_get_json(f"{base}/api/workspaces/{ws}/versions", _auth_headers())
    except ObservationError as exc:
        return {"workspace": ws, "observed": None, "error": str(exc)}
    if status != 200:
        return {"workspace": ws, "observed": None,
                "error": f"GET /api/workspaces/{ws}/versions returned {status}"}
    observed = ((body or {}).get("workspace") or {}).get("current")
    return {"workspace": ws, "observed": observed, "error": None}


def _last_seen_sort_key(host: dict) -> float:
    """Coerce ``last_seen_at`` to a plain float before comparing — a bare
    ``h.get("last_seen_at") or 0`` mixes a real timestamp (whatever type
    aw-backend happens to serialise it as) against the int fallback `0`,
    and Python's `max()` raises TypeError the moment one host in the list
    has a string/ISO value and another has None. Every key is now
    guaranteed comparable, so a malformed value degrades to "oldest", not
    a crash that `_observe`'s generic except turns into a silent, wrong
    "unknown"."""
    value = host.get("last_seen_at")
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def observe_remote_host(_name: str, workspace: str) -> dict:
    """Live ``RemoteHost.cli_version`` heartbeat via aw-backend's
    ``GET /api/workspaces/{slug}/remote-hosts`` (bulk; filtered client-side —
    no single-host-by-slug route exists)."""
    ws = workspace or _this_workspace()
    if ws != _this_workspace():
        return _cross_tenant_unknown(ws, "AGENTS_PLATFORM_TOKEN")
    base = _backend_base()
    if not base:
        return {"workspace": ws, "observed": None,
                "error": f"{_BACKEND_URL_VAR} is not set for this MCP upstream"}
    try:
        status, body = _http_get_json(f"{base}/api/workspaces/{ws}/remote-hosts",
                                      _auth_headers())
    except ObservationError as exc:
        return {"workspace": ws, "observed": None, "error": str(exc)}
    if status != 200:
        return {"workspace": ws, "observed": None,
                "error": f"GET /api/workspaces/{ws}/remote-hosts returned {status}"}
    hosts = (body or {}).get("hosts") or []
    if not hosts:
        return {"workspace": ws, "observed": None,
                "error": f"no remote host linked to workspace {ws!r}"}
    host = max(hosts, key=_last_seen_sort_key)
    return {"workspace": ws, "observed": host.get("cli_version"), "error": None}


def observe_service(name: str, workspace: str) -> dict:
    """No live observation path exists yet for service-class components in
    this app's runtime (see module docstring) — always ``unknown``, never a
    guess. A future wiring (remote_host_exec_run / SSH, per the
    aw-autoskill-verify-deploy-in-container skill) replaces this stub."""
    ws = workspace or _this_workspace()
    return {"workspace": ws, "observed": None,
            "error": f"no live observation adapter for service:{name} in this runtime "
                     "(needs container/SSH access this app does not have) — treat as unknown"}


ADAPTERS: dict[str, Callable[[str, str], dict]] = {
    "app": observe_app,
    "core": observe_core,
    "remote-host": observe_remote_host,
    "service": observe_service,
}


def _observe(klass: str, name: str, workspace: str) -> dict:
    adapter = ADAPTERS.get(klass)
    if adapter is None:
        return {"workspace": workspace or _this_workspace(), "observed": None,
                "error": f"no adapter for component class {klass!r}"}
    try:
        return adapter(name, workspace)
    except Exception as exc:  # pragma: no cover - defensive, per workspace
        log.warning("deploy_reconcile: observe(%s, %s, %s) failed", klass, name, workspace,
                    exc_info=True)
        return {"workspace": workspace or _this_workspace(), "observed": None, "error": str(exc)}


# ── per-card validation + evaluation ─────────────────────────────────────

def _validate(component: str, to_version: str) -> list[str]:
    reasons = []
    if not component:
        reasons.append("DeployComponent is empty")
    else:
        klass, name = parse_component(component)
        if klass is None:
            reasons.append(f"DeployComponent {component!r} doesn't match any known class "
                           "(app:<slug> | service:<name> | core | remote-host)")
        elif klass in ("app", "service") and not name:
            reasons.append(f"DeployComponent {component!r} has no name after the prefix")
    if not to_version:
        reasons.append("DeployToVersion is empty")
    return reasons


def _aggregate_verdict(observations: list[dict]) -> str:
    verdicts = {o["verdict"] for o in observations}
    if "unknown" in verdicts:
        return "unknown"
    if verdicts == {"deployed"}:
        return "deployed"
    if "deployed" not in verdicts:
        return "pending"
    return "partial"


def _evaluate_card(board: "kanban_dispatch.BoardClient", summary: dict) -> dict:
    page_id = summary.get("page_id") or ""
    props = board.properties(page_id, DEPLOY_PROPS)
    title = props.get("Name") or summary.get("title") or ""
    status = props.get("Status") or summary.get("status") or ""
    target_slug = (props.get("TargetSlug") or "").strip()
    qa_status = (props.get("QAStatus") or "").strip()
    component = (props.get("DeployComponent") or "").strip()
    env = (props.get("DeployEnv") or "prod").strip() or "prod"
    to_version = (props.get("DeployToVersion") or "").strip()
    from_version = (props.get("DeployFromVersion") or "").strip()
    workspaces = props.get("DeployWorkspaces") or []
    if isinstance(workspaces, str):
        workspaces = [workspaces] if workspaces else []

    prior_raw = props.get("DeployCheckResult") or ""
    try:
        prior = json.loads(prior_raw) if prior_raw else {}
    except ValueError:
        prior = {}

    row: dict[str, Any] = {
        "page_id": page_id, "title": title, "status": status, "target_slug": target_slug,
        "env": env, "component": component, "to_version": to_version,
    }

    reasons = _validate(component, to_version)
    if reasons:
        row.update(eligible=False, verdict="invalid", action="none", reasons=reasons,
                   observations=[])
        row["already_reported"] = bool(prior.get("invalid_commented"))
        # Carry forward, don't preset True: this stamp is written BEFORE the
        # comment is attempted (see check_deployed), so presetting True here
        # would permanently mark "commented" even if the comment call itself
        # then fails — mark_invalid_commented() flips it, only after a
        # confirmed success.
        row["_stamp"] = {"verdict": "invalid", "invalid_commented": row["already_reported"],
                         "unknown_streak": 0}
        return row

    klass, name = parse_component(component)
    target_workspaces = workspaces or [_this_workspace()]
    observations = []
    for ws in target_workspaces:
        obs = _observe(klass, name, ws)
        verdict_ws = "unknown" if obs.get("error") else compare(klass, obs.get("observed"),
                                                                to_version)
        observations.append({"workspace": ws, "observed": obs.get("observed"),
                             "verdict": verdict_ws, "reason": obs.get("error")})

    verdict = _aggregate_verdict(observations)

    prior_verdict = prior.get("verdict")
    if prior_verdict == "deployed" and verdict in ("pending", "partial"):
        verdict = "rolled_back"
    elif verdict in ("pending", "partial") and from_version:
        below_from = all(compare(klass, o.get("observed"), from_version) == "pending"
                         for o in observations if o.get("observed"))
        if below_from and any(o.get("observed") for o in observations):
            verdict = "rolled_back"

    streak = (prior.get("unknown_streak") or 0) + 1 if verdict == "unknown" else 0

    # Whether a QA dispatch is still owed for this card. Deliberately NOT
    # gated on the Status-transition guard below (status != "Testing") —
    # that guard is what makes the MOVE idempotent, but it also means a
    # card already sitting in Testing from a prior pass would never be
    # looked at again. `qa_dispatched` only flips true once the caller
    # (mcp_server.py, which owns the actual platform dispatch call)
    # confirms success via mark_qa_dispatched() — so a failed POST here
    # keeps this true on every subsequent pass until one succeeds, instead
    # of being silently dropped the moment the card's Status changes.
    qa_dispatched = bool(prior.get("qa_dispatched"))
    qa_dispatch_needed = (verdict == "deployed" and qa_status.strip().lower() != "done"
                         and not qa_dispatched)

    row.update(eligible=True, verdict=verdict, observations=observations, action="none",
              qa_dispatch_needed=qa_dispatch_needed)
    row["_stamp"] = {"verdict": verdict, "unknown_streak": streak,
                     "invalid_commented": False, "qa_dispatched": qa_dispatched}
    row["_qa_status"] = qa_status
    row["_streak"] = streak
    return row


def _stamp(board: "kanban_dispatch.BoardClient", page_id: str, stamp: dict) -> None:
    import datetime as _dt
    board.set_property(page_id, "DeployCheckResult", json.dumps(stamp))
    board.set_property(page_id, "LastDeployCheckAt",
                       _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))


def _mark_stamp_field(board: "kanban_dispatch.BoardClient", page_id: str, field: str,
                      value: Any) -> None:
    """Read-modify-write a single field on DeployCheckResult, only after the
    caller has confirmed the thing it describes actually succeeded — so a
    failure leaves the field unset and the NEXT `check_deployed()` pass
    retries, instead of a pre-set flag silently and permanently skipping it."""
    current = board.properties(page_id, ["DeployCheckResult"]).get("DeployCheckResult") or ""
    try:
        stamp = json.loads(current) if current else {}
    except ValueError:
        stamp = {}
    stamp[field] = value
    board.set_property(page_id, "DeployCheckResult", json.dumps(stamp))


def mark_qa_dispatched(board: "kanban_dispatch.BoardClient", page_id: str) -> None:
    """Record that a QA dispatch for this card's current `deployed` verdict
    actually succeeded — called by the caller that owns the platform POST
    (mcp_server.py), only on a 200. See `qa_dispatch_needed` above."""
    _mark_stamp_field(board, page_id, "qa_dispatched", True)


def mark_invalid_commented(board: "kanban_dispatch.BoardClient", page_id: str) -> None:
    """Record that the one-time 'invalid' comment for this card actually
    posted — called only after a confirmed success, so a failed post retries
    next pass instead of _evaluate_card's `already_reported` permanently
    silencing it (the stamp used to be written unconditionally before the
    comment was even attempted)."""
    _mark_stamp_field(board, page_id, "invalid_commented", True)


def _evidence(row: dict) -> str:
    lines = [f"component={row['component']} env={row['env']} to_version={row['to_version']}"]
    for o in row.get("observations", []):
        detail = o.get("observed") or o.get("reason") or "?"
        lines.append(f"- workspace={o['workspace']}: {o['verdict']} ({detail})")
    return "\n".join(lines)


def check_deployed(board: "kanban_dispatch.BoardClient", *, page_id: str | None = None,
                   dry_run: bool = False, max_cards: int = DEFAULT_MAX_CARDS) -> dict:
    """The full tool contract. Synchronous — every call here is a plain
    HTTP round trip, same as ``kanban_dispatch``'s board calls (its async
    callers wrap it in ``asyncio.to_thread``).

    Returns moved cards, still-waiting cards, counts, errors and an explicit
    truncation/continuation signal — never just a per-card action list, so
    the calling LLM gets the complete lane picture in one call.
    """
    candidates: list[dict] = []
    truncated = False
    if page_id:
        candidates.append({"page_id": page_id})
    else:
        for status in DEPLOY_STATUSES:
            cards = board.ready_cards(status=status, limit=max_cards + 1)
            if len(cards) > max_cards:
                truncated = True
                cards = cards[:max_cards]
            candidates.extend(cards)

    moved: list[dict] = []
    still_waiting: list[dict] = []
    errors: list[dict] = []
    counts: dict[str, int] = {}

    for summary in candidates:
        pid = summary.get("page_id") or ""
        try:
            row = _evaluate_card(board, summary)
        except kanban_dispatch.BoardUnavailable as exc:
            errors.append({"page_id": pid, "title": summary.get("title"), "error": str(exc)})
            counts["errors"] = counts.get("errors", 0) + 1
            continue
        except Exception as exc:  # pragma: no cover - defensive, per card
            log.warning("deploy_reconcile: card %s failed", pid, exc_info=True)
            errors.append({"page_id": pid, "title": summary.get("title"), "error": str(exc)})
            counts["errors"] = counts.get("errors", 0) + 1
            continue

        stamp = row.pop("_stamp")
        qa_status = row.pop("_qa_status", "")
        streak = row.pop("_streak", 0)

        if not dry_run:
            try:
                _stamp(board, pid, stamp)
            except kanban_dispatch.BoardUnavailable as exc:
                row["stamp_failed"] = str(exc)

        verdict = row["verdict"]
        moved_entry = None

        # Guarded per card, same as _evaluate_card/_stamp above: a transient
        # BoardUnavailable (Notion 5xx/timeout) on one card's comment/move
        # must not propagate out of this loop and collapse the whole pass's
        # already-computed moved/still_waiting/counts into a single error —
        # code-review finding on PR #25. Caught, recorded on the row, and
        # this card simply retries next pass (nothing it already stamped
        # above gates that retry — see mark_invalid_commented/
        # mark_qa_dispatched, both written only on confirmed success).
        try:
            if dry_run:
                pass
            elif verdict == "invalid":
                if not row.pop("already_reported", False):
                    board.add_comment(pid, "🤖 kanban_check_deployed: invalid — " + "; ".join(
                        row.get("reasons", [])))
                    mark_invalid_commented(board, pid)
            elif verdict == "unknown":
                if streak >= UNKNOWN_STREAK_LIMIT and row["status"] != "Need Human":
                    # move_card() requires a comment for need_human and posts
                    # it as part of the SAME call — a separate add_comment()
                    # first would either double-post or (as it did before
                    # this fix) go through while the comment-less move()
                    # silently no-ops, since move_card() returns {"ok":
                    # False, ...} rather than raising.
                    board.move(pid, "need_human", comment=(
                        f"🤖 kanban_check_deployed: {streak} consecutive unknown checks — "
                        "escalating.\n\n" + _evidence(row)))
                    row["action"] = "needs_human"
                    moved_entry = {"previous_status": row["status"], "new_status": "Need Human"}
            elif verdict == "rolled_back":
                if row["status"] != "Need Human":
                    board.move(pid, "need_human", comment=(
                        "🤖 kanban_check_deployed: rollback detected — the observed "
                        "version dropped below a previously-deployed target.\n\n"
                        + _evidence(row)))
                    row["action"] = "needs_human"
                    moved_entry = {"previous_status": row["status"], "new_status": "Need Human"}
            elif verdict == "deployed":
                if qa_status.strip().lower() == "done":
                    if row["status"] != "Done":
                        board.add_comment(pid, "🤖 kanban_check_deployed: deployed, QA already "
                                         "done — moving to Done.\n\n" + _evidence(row))
                        board.move(pid, "done")
                        row["action"] = "moved_done"
                        moved_entry = {"previous_status": row["status"], "new_status": "Done"}
                else:
                    if row["status"] != "Testing":
                        board.add_comment(pid, "🤖 kanban_check_deployed: deployed — moving to "
                                         "Testing for post-deployment QA.\n\n" + _evidence(row))
                        board.move(pid, "Testing")
                        row["action"] = "moved_testing"
                        moved_entry = {"previous_status": row["status"], "new_status": "Testing"}
            # pending / partial: stays put, action stays "none"
        except kanban_dispatch.BoardUnavailable as exc:
            row["board_action_failed"] = str(exc)
        except Exception as exc:  # pragma: no cover - defensive, per card
            # Mirrors the _evaluate_card guard above: BoardUnavailable is the
            # expected/typed shape, but a board call three layers below this
            # (e.g. a bare socket.timeout that pre-dates kanban_dispatch's own
            # OSError translation) must still be bounded to this one card
            # rather than killing the whole batch's already-computed results.
            log.warning("deploy_reconcile: card %s action failed", pid, exc_info=True)
            row["board_action_failed"] = str(exc)

        if moved_entry:
            moved.append({**row, **moved_entry})
        else:
            row["reason"] = row.get("reasons") or [
                f"verdict={verdict}; staying in {row['status']}"]
            still_waiting.append(row)

        counts[row["action"]] = counts.get(row["action"], 0) + 1
        counts[f"verdict:{verdict}"] = counts.get(f"verdict:{verdict}", 0) + 1

    return {
        "ok": True,
        "dry_run": dry_run,
        "considered": len(candidates),
        "truncated": truncated,
        "counts": counts,
        "moved": moved,
        "still_waiting": still_waiting,
        "errors": errors,
        "continuation": ({"has_more": True,
                         "hint": f"lane has more than {max_cards} cards — re-invoke with a "
                                 "higher max_cards for full reconciliation"}
                        if truncated else {"has_more": False}),
    }


__all__ = ["check_deployed", "parse_component", "parse_semver", "looks_like_sha", "compare",
           "observe_app", "observe_core", "observe_remote_host", "observe_service",
           "ADAPTERS", "DEPLOY_STATUSES", "DEFAULT_MAX_CARDS"]
