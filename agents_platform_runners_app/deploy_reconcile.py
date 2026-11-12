"""``kanban_check_deployed`` — deterministic Ready-to-Deploy reconciliation.

Architect decision, Kanban card 4165bf3b-9510-8162-89d0-dca46ba66481, extended
by the v2 design on card 4165bf3b-9510-812c (this module's C3 slice). The
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

**Observation precedence (C3)**: for each (component, workspace) pair, three
sources are tried in order and the evidence always names the one used, so a
wrong verdict is diagnosable from the card alone — live adapter (asks the
running thing right now) > a fresh row from aw-backend's push-fed component
inventory (``GET /api/workspaces/{slug}/components``, fetched ONCE per pass,
never per card) > a fresh (<12h) agent observation left on a prior pass's
``DeployCheckResult`` by the (not-yet-built, C5) ``kanban_report_deploy_
observation`` tool. All three failing is ``unknown``, never a guess.

**Known gaps, by component class** (say so rather than guess — the
architect's own fail-closed rule): ``service:<name>`` has no live
observation adapter wired yet (would need SSH/docker-exec per the
``aw-autoskill-verify-deploy-in-container`` skill, not available to this
app's runtime) — it falls through to the inventory/agent tiers above, and
only resolves ``unknown`` once those are also exhausted. Cross-tenant
``DeployWorkspaces`` entries (naming a workspace other than this one) skip
both the live adapter and the inventory (scoped to this workspace only —
every credential this app holds, ``AW_WORKSPACE_API_KEY`` /
``AGENTS_PLATFORM_TOKEN``, is scoped to exactly one workspace membership) and
go straight to the agent-evidence tier.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import kanban_dispatch

log = logging.getLogger("aw_apps.agents_platform_runners.deploy_reconcile")

DEPLOY_STATUSES = ("Ready to Deploy", "Testing")
UNKNOWN_STREAK_LIMIT = 3
DEFAULT_MAX_CARDS = 25
NO_DEPLOY_SENTINEL = "none"
AGENT_EVIDENCE_STALE_AFTER_S = 12 * 3600

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


# ── inventory (fresh row, tier 2) + agent evidence (tier 3) ─────────────

def _inventory_fetch(workspace: str) -> tuple[dict[str, dict], str | None]:
    """ONE GET per pass for the whole lane (architect decision, C1/C3) —
    never per card. Returns ``({component: row}, None)`` on success, or
    ``({}, error)`` on any failure: a dead/unreachable/malformed inventory
    must never block the pass, it just leaves every card to fall through to
    the live adapter/agent-evidence tiers (same fail-open-to-the-next-tier
    shape as ``observe_service``'s stub). Code-review finding on PR #33
    (reported twice): a 200 response with a non-JSON or wrongly-shaped body
    raised uncaught from here, unguarded at the top of ``check_deployed()``
    — ahead of every per-card try/except — and crashed the ENTIRE pass for
    every card in the lane, not just the one inventory lookup that failed."""
    base = _backend_base()
    if not base:
        return {}, f"{_BACKEND_URL_VAR} is not set for this MCP upstream"
    try:
        status, body = _http_get_json(f"{base}/api/workspaces/{workspace}/components",
                                      _auth_headers())
    except ObservationError as exc:
        return {}, str(exc)
    except ValueError as exc:
        return {}, f"malformed inventory response: {exc}"
    if status != 200:
        return {}, f"GET /api/workspaces/{workspace}/components returned {status}"
    try:
        rows = (body or {}).get("components") or []
        return ({r["component"]: r for r in rows if isinstance(r, dict) and r.get("component")},
               None)
    except (AttributeError, TypeError) as exc:
        return {}, f"malformed inventory response shape: {exc}"


def _parse_iso_datetime(value: str | None) -> float | None:
    """Epoch seconds, or ``None`` for anything that doesn't parse — a
    datetime stamp must be parsed defensively (architect risk #2): a
    date-only value (``datetime.fromisoformat`` happily accepts one as
    midnight) must never be mistaken for a fresh timestamp, so a missing
    time component is rejected explicitly rather than silently defaulting."""
    if not value:
        return None
    text = value.strip()
    if "T" not in text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _fresh_agent_observation(prior: dict, now: float, workspace: str) -> dict | None:
    """The lowest-trust tier: an observation a prior pass's
    ``kanban_report_deploy_observation`` call (C5) left on this card's own
    ``DeployCheckResult``, valid for 12h from its own ``at`` stamp. Returns
    ``None`` (never a guess) when absent, shapeless, its timestamp can't be
    parsed (an unparseable age is not a fresh one), or — code-review finding
    on PR #33 — it was recorded for a DIFFERENT workspace: a card's
    ``agent_observation`` is a single blob, not one per ``DeployWorkspaces``
    entry, so without this check the same observation would get applied to
    every workspace on a multi-workspace card, including ones it was never
    actually about. An observation with no ``workspace`` of its own is
    treated as being about this app's own workspace — the only one a
    workspace-less card (the common, single-workspace case) ever resolves."""
    obs = prior.get("agent_observation")
    if not isinstance(obs, dict) or not obs.get("observed"):
        return None
    if (obs.get("workspace") or _this_workspace()) != workspace:
        return None
    at = _parse_iso_datetime(obs.get("at"))
    if at is None:
        return None
    age_s = now - at
    if age_s < 0 or age_s > AGENT_EVIDENCE_STALE_AFTER_S:
        return None
    return {**obs, "age_s": age_s}


def _resolve_observation(klass: str | None, name: str, component: str, workspace: str,
                         to_version: str, *, inventory: dict[str, dict], prior: dict,
                         now: float) -> dict:
    """One (component, workspace) pair's observation, source-labelled.
    Precedence: live adapter > fresh inventory row > fresh agent evidence >
    unknown — see module docstring. Always returns the source used (or
    ``None`` for unknown) so the evidence string can name it. A fresh
    inventory row with no ``version`` (code-review finding on PR #33: a
    component registered before its version field is backfilled is a
    plausible real shape) is treated the same as a missing row — it falls
    through to agent evidence rather than returning a bogus 'unknown' via
    ``source="inventory"``."""
    live = _observe(klass, name, workspace)
    if not live.get("error") and live.get("observed"):
        return {"workspace": workspace, "observed": live["observed"], "source": "live",
                "reason": None, "verdict": compare(klass, live["observed"], to_version)}
    live_error = live.get("error")

    inv_row = inventory.get(component) if workspace == _this_workspace() else None
    if inv_row is not None and not inv_row.get("stale") and inv_row.get("version"):
        return {"workspace": workspace, "observed": inv_row.get("version"),
                "source": "inventory", "age_s": inv_row.get("age_s"), "reason": None,
                "verdict": compare(klass, inv_row.get("version"), to_version)}

    agent_obs = _fresh_agent_observation(prior, now, workspace)
    if agent_obs is not None:
        return {"workspace": workspace, "observed": agent_obs.get("observed"),
                "source": "agent", "age_s": agent_obs.get("age_s"), "reason": None,
                "verdict": compare(klass, agent_obs.get("observed"), to_version)}

    if inv_row is not None:
        why = ("stale" if inv_row.get("stale") else "has no version")
        reason = (f"inventory row {why} (age_s={inv_row.get('age_s')}); "
                  f"live adapter: {live_error}")
    else:
        reason = live_error or ("no observation source available (no live adapter, "
                                "inventory row, or fresh agent evidence)")
    return {"workspace": workspace, "observed": None, "source": None, "reason": reason,
            "verdict": "unknown"}


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


def _evaluate_card(board: "kanban_dispatch.BoardClient", summary: dict, *,
                   inventory: dict[str, dict], now: float) -> dict:
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

    if component.lower() == NO_DEPLOY_SENTINEL:
        # Explicit no-deployment-needed: never validated against a version,
        # never observed — the card itself IS the evidence. Routes purely on
        # QAStatus, same shape as the "deployed" verdict's own QA gate below.
        qa_dispatched = bool(prior.get("qa_dispatched"))
        row.update(eligible=True, verdict="no_deploy_needed", observations=[], action="none",
                  qa_dispatch_needed=(qa_status.strip().lower() != "done" and not qa_dispatched))
        row["_stamp"] = {"verdict": "no_deploy_needed", "unknown_streak": 0,
                         "invalid_commented": False, "qa_dispatched": qa_dispatched}
        row["_qa_status"] = qa_status
        row["_streak"] = 0
        return row

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
    observations = [
        _resolve_observation(klass, name, component, ws, to_version,
                             inventory=inventory, prior=prior, now=now)
        for ws in target_workspaces
    ]

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
    if row.get("verdict") == "no_deploy_needed":
        return f"component=none — explicit no-deployment-needed sentinel (env={row['env']})"
    lines = [f"component={row['component']} env={row['env']} to_version={row['to_version']}"]
    for o in row.get("observations", []):
        detail = o.get("observed") or o.get("reason") or "?"
        extras = []
        if o.get("source"):
            extras.append(f"source={o['source']}")
        age = o.get("age_s")
        if isinstance(age, (int, float)):
            extras.append(f"age={age:.0f}s")
        extra_txt = (", " + ", ".join(extras)) if extras else ""
        lines.append(f"- workspace={o['workspace']}: {o['verdict']} ({detail}{extra_txt})")
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
    now = time.time()
    # ONE GET per pass for the whole lane (architect decision) — never per
    # card. A failed/unreachable inventory is reported, not raised: every
    # card still gets evaluated, just without that tier (falls through to
    # the live adapter/agent-evidence precedence in _resolve_observation).
    inventory, inventory_error = _inventory_fetch(_this_workspace())

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
            row = _evaluate_card(board, summary, inventory=inventory, now=now)
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
            elif verdict == "no_deploy_needed":
                # Same QAStatus gate as "deployed" above — the sentinel
                # means "no deployment ever needed", not "already verified".
                if qa_status.strip().lower() == "done":
                    if row["status"] != "Done":
                        board.add_comment(pid, "🤖 kanban_check_deployed: no deployment needed "
                                         "(DeployComponent=none), QA already done — moving to "
                                         "Done.\n\n" + _evidence(row))
                        board.move(pid, "done")
                        row["action"] = "moved_done"
                        moved_entry = {"previous_status": row["status"], "new_status": "Done"}
                else:
                    if row["status"] != "Testing":
                        board.add_comment(pid, "🤖 kanban_check_deployed: no deployment needed "
                                         "(DeployComponent=none) — moving to Testing for QA "
                                         "sign-off.\n\n" + _evidence(row))
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
        "inventory_error": inventory_error,
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
           "ADAPTERS", "DEPLOY_STATUSES", "DEFAULT_MAX_CARDS", "NO_DEPLOY_SENTINEL",
           "AGENT_EVIDENCE_STALE_AFTER_S"]
