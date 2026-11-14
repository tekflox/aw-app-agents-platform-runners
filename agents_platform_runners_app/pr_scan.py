"""``pr_scan`` — the ``prs`` work_loop section's own internals: cloned-repo
enumeration, the two-phase ``gh`` sweep, the stalled/CI decision functions,
and the per-PR "viewed" back-off store.

Architect decision, Kanban card 4175bf3b-9510-8163 (phase 2 of the work_loop
design, 4165bf3b-9510-810d) — mirrors the ``deploy_reconcile.py`` split: all
of the actual logic lives here, pure where possible, and ``work_loop.py``'s
``_prs_loop`` is a thin wrapper (same relationship ``_deploy_loop`` has with
``check_deployed``).

**Where this module actually runs (gateway-boundary follow-up,
card 4175bf3b):** ``scan_prs`` is NOT the callable injected onto
``WorkLoopContext.scanner`` — that would put it inside ``mcp_server.py``'s
stdio-child process, which the mcp-gateway container spawns with neither
``/opt/aw-workspace/repos`` nor ``AW_WORKSPACE_HOME`` nor any ``gh``/``git``
credential mounted (confirmed live: it silently returned zero repos every
call). ``scan_prs`` runs one hop further away instead, inside this app's own
core-side ``POST /pr-scan`` route (``routes.py``), which IS where
``/opt/aw-workspace/repos`` and ``gh`` auth genuinely exist. The actual
``ctx.scanner`` is ``mcp_server.py``'s ``_prs_scan_via_route`` — an HTTP
closure to that route, the same two-hop pattern ``list_warm_containers``/
``search_execution_history`` already use for this exact container boundary.

Two-phase sweep instead of one ``gh pr list`` per cloned repo (~90 of them,
almost all returning nothing):

* **Phase A** — one ``gh search prs --owner <o> --state open`` call per
  distinct GitHub owner among the cloned repos, filtered back down to just
  the cloned repo set. Cheap: 1 call per owner, not per repo.
* **Phase B** — ``gh pr view <url> --json headRefOid,updatedAt,isDraft,
  statusCheckRollup`` only for candidates that already survive the cheap,
  no-network gates (not a draft, old enough, back-off eligible) — capped at
  the work-order ``limit`` BEFORE probing, never after, with ``truncated``
  reported honestly when the cap bites.

Nothing here makes an LLM call or decides anything beyond detect+tag — a
stalled PR becomes one Mode B work order for the Manager (or whichever
caller agent) to actually act on; the only side effect this module takes by
itself is the viewed-stamp write, exactly as it does for the attention
loop's LastSweptAt (design §2) — because there is no Notion card to stamp
this against.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import subprocess
import tempfile
from typing import Any, Awaitable, Callable

from . import deploy_reconcile

log = logging.getLogger("aw_apps.agents_platform_runners.pr_scan")

CONTAINER_DIR = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
REPOS_DIR = os.path.join(CONTAINER_DIR, "repos")

VIEWED_STORE_REL = "data/agents-platform-runners/work_loop_prs_viewed.json"

GH_TIMEOUT_S = 30
GH_MAX_CONCURRENCY = 4

# Used only as this module's own standalone default (e.g. direct testing) —
# the real threshold a work_loop call actually applies is work_loop.py's own
# ``PR_STALL_HOURS``, passed in explicitly on every call so that constant
# stays the single source of truth.
DEFAULT_STALL_HOURS = 6

SEARCH_FIELDS = "number,repository,title,url,updatedAt,isDraft,createdAt,author"
VIEW_FIELDS = "headRefOid,updatedAt,isDraft,statusCheckRollup,labels"

# `fredericowu/aw-shared-workflows`'s code-review round state machine only
# auto-re-reviews a `synchronize` push when the PR is in round 2
# (`devteam:awaiting-fixes`) — a PR already escalated to this label does
# NOT get a fresh review just because someone pushed a fix (see
# skills/aw-agent-coder/SKILL.md, "A push to a devteam:needs-human PR does
# not auto-retrigger review"). Card 4175bf3b: rather than touching that
# 45-repo-wide shared workflow, force the review mechanically from here,
# something this module already fully controls.
NEEDS_HUMAN_LABEL = "devteam:needs-human"

# Code-review finding (PR #41): `gh search prs` defaults to 30 results
# sorted by best-match relevance, not `updatedAt` — for an owner with more
# open PRs than that (across ALL their repos, not just the cloned ones; the
# cloned-repo filter happens client-side after this fetch), the oldest/
# most-neglected ones are exactly the ones most likely to fall outside that
# window, undermining the whole point of a staleness sweep, silently (no
# error, and the response's own `truncated` flag only covers the Phase B
# probe cap, not this one). Sorting oldest-updated-first means a PR that
# DOES get cut off by the limit is, by construction, one of the LEAST
# stale ones in the owner's queue — the opposite of what this loop cares
# about. 1000 is `gh`'s own practical ceiling for one search call.
SEARCH_LIMIT = 1000

# GitHub's *live* check states — "status", never "conclusion": a SKIPPED or
# NEUTRAL check is status=COMPLETED, so a conclusion-based "is CI running"
# read would misclassify it as still running (design §7, risk #2).
_LIVE_CHECK_STATUSES = {"QUEUED", "IN_PROGRESS", "PENDING"}

_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_SSH_URL_RE = re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_HTTPS_RE = re.compile(r"^https://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


GhRunner = Callable[[list], Awaitable["tuple[int, str, str]"]]
"""``async (argv_after_gh) -> (returncode, stdout, stderr)``. Never raises —
a missing binary or a timeout comes back as a non-zero code with the detail
in ``stderr``, so every caller here treats every failure mode identically.
Injectable so tests never shell out to a real ``gh``."""


async def default_gh_runner(args: list) -> "tuple[int, str, str]":
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", "gh: command not found"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh {' '.join(args)} timed out after {GH_TIMEOUT_S}s"
    return (proc.returncode or 0), stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _git_remote_url(repo_path: str) -> "str | None":
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                             capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# ── repo enumeration ─────────────────────────────────────────────────────

def parse_github_remote(url: str) -> "tuple[str, str] | None":
    """``(owner, repo)`` for a github.com ssh or https remote, or ``None``
    for anything else (a non-GitHub origin, a malformed URL, or empty)."""
    text = (url or "").strip()
    if not text:
        return None
    for pattern in (_SSH_RE, _SSH_URL_RE, _HTTPS_RE):
        m = pattern.match(text)
        if m:
            return m.group("owner"), m.group("repo")
    return None


def map_cloned_repos(remotes: dict) -> dict:
    """Pure. ``remotes`` is ``{dirname: origin_url_or_None}`` — ``None``
    stands for "no ``.git``" or "remote unreadable", already collapsed by
    the caller. Dedups by ``(owner, repo)`` (two local dirs cloning the same
    GitHub repo count once); everything that didn't map to a GitHub
    ``owner/repo`` is reported in ``unmapped`` rather than silently
    dropped."""
    repos: list = []
    seen: set = set()
    unmapped: list = []
    for dirname in sorted(remotes):
        url = remotes[dirname]
        parsed = parse_github_remote(url) if url else None
        if parsed is None:
            unmapped.append(dirname)
            continue
        owner, repo = parsed
        if (owner, repo) in seen:
            continue
        seen.add((owner, repo))
        repos.append({"owner": owner, "repo": repo, "path": dirname})
    owners = sorted({r["owner"] for r in repos})
    return {"repos": repos, "owners": owners, "unmapped": unmapped}


def scan_cloned_repos(repos_dir: str = REPOS_DIR, *,
                      git_remote_url: "Callable[[str], str | None]" = _git_remote_url) -> dict:
    """Walks ``repos_dir`` for immediate subdirectories with a ``.git``,
    resolves each one's ``origin`` remote, and maps the result via
    ``map_cloned_repos``. The filesystem/subprocess side is not pure
    (injectable via ``git_remote_url`` for tests) but contributes nothing
    beyond gathering ``{dirname: url}`` — every actual decision is in
    ``map_cloned_repos``."""
    try:
        entries = sorted(os.listdir(repos_dir))
    except OSError as exc:
        # Code-review finding (card 4175bf3b gateway-boundary follow-up): a
        # silent {"repos": [], "owners": [], "unmapped": []} here is
        # indistinguishable from "this workspace genuinely has zero cloned
        # repos" — exactly the shape that let the gateway-boundary gap run
        # in production reporting ok:true with nothing to show for it, with
        # no error anywhere to say why. Carry the failure forward so
        # ``scan_prs`` can put it in the section's own ``errors`` instead
        # of lying about an empty-but-healthy scan.
        result = map_cloned_repos({})
        result["error"] = f"{repos_dir} unreadable: {exc}"
        return result
    remotes: dict = {}
    for name in entries:
        full = os.path.join(repos_dir, name)
        if not os.path.isdir(os.path.join(full, ".git")):
            continue
        remotes[name] = git_remote_url(full)
    return map_cloned_repos(remotes)


# ── gh calls ──────────────────────────────────────────────────────────────

async def search_open_prs_for_owner(owner: str, gh_runner: GhRunner) -> "tuple[list | None, str | None]":
    code, out, err = await gh_runner(["search", "prs", "--owner", owner, "--state", "open",
                                      "--sort", "updated", "--order", "asc",
                                      "--limit", str(SEARCH_LIMIT), "--json", SEARCH_FIELDS])
    if code != 0:
        return None, (err or out or f"gh search prs --owner {owner} exited {code}").strip()
    try:
        rows = json.loads(out or "[]")
    except ValueError as exc:
        return None, f"malformed gh search prs output: {exc}"
    return (rows if isinstance(rows, list) else []), None


async def view_pr(url: str, gh_runner: GhRunner) -> "tuple[dict | None, str | None]":
    code, out, err = await gh_runner(["pr", "view", url, "--json", VIEW_FIELDS])
    if code != 0:
        return None, (err or out or f"gh pr view {url} exited {code}").strip()
    try:
        row = json.loads(out or "{}")
    except ValueError as exc:
        return None, f"malformed gh pr view output: {exc}"
    return (row if isinstance(row, dict) else {}), None


async def post_review_comment(url: str, gh_runner: GhRunner) -> "str | None":
    """Posts ``/aw-review`` as a PR comment to force a review run. Returns
    ``None`` on success, or an error string on failure — same shape as
    ``view_pr``/``search_open_prs_for_owner``."""
    code, out, err = await gh_runner(["pr", "comment", url, "--body", "/aw-review"])
    if code != 0:
        return (err or out or f"gh pr comment {url} exited {code}").strip()
    return None


async def _bounded_gather(items: list, worker, *, concurrency: int) -> list:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item):
        async with sem:
            return await worker(item)

    return await asyncio.gather(*(_one(item) for item in items))


def filter_prs_to_cloned_repos(prs: list, cloned_full_names: "set[str]") -> list:
    """Pure. Keeps only PRs whose ``repository.nameWithOwner`` is in the
    cloned repo set — the Phase A result is one owner-wide search, so this
    is what narrows it back down to repos actually checked out here."""
    out = []
    for pr in prs:
        full_name = ((pr.get("repository") or {}).get("nameWithOwner") or "")
        if full_name in cloned_full_names:
            out.append(pr)
    return out


# ── decision functions (pure) ────────────────────────────────────────────

def no_live_or_recent_ci(status_check_rollup: "list | None", now: float, *, stall_hours: float) -> bool:
    """True iff HEAD has no currently-running check AND nothing completed
    within the stall window. A null/empty rollup (repo has no CI at all) is
    trivially "no CI" — a subset of "no recent CI" — so it passes."""
    rollup = status_check_rollup or []
    if any((c.get("status") or "").upper() in _LIVE_CHECK_STATUSES for c in rollup):
        return False
    completed_ats = [t for t in
                     (deploy_reconcile._parse_iso_datetime(c.get("completedAt")) for c in rollup)
                     if t is not None]
    if not completed_ats:
        return True
    newest = max(completed_ats)
    return (now - newest) / 3600.0 >= stall_hours


def backoff_state(current_updated_at: "str | None", stamp: "dict | None") -> str:
    """``"eligible"`` | ``"backed_off"`` — symmetric with
    ``work_loop._backoff_state``'s semantics, a different store underneath.
    No stamp yet, or the PR has moved (current ``updatedAt`` strictly newer
    than the stamped one) since the stamp was written -> eligible again (the
    stall-hours gate still has to pass again on top of that). Fails OPEN on
    an unparseable timestamp on either side — same rule as the attention
    loop's own back-off check."""
    if not stamp:
        return "eligible"
    stamped_updated = stamp.get("updated_at")
    if not stamped_updated or not current_updated_at:
        return "eligible"
    cur = deploy_reconcile._parse_iso_datetime(current_updated_at)
    stamped = deploy_reconcile._parse_iso_datetime(stamped_updated)
    if cur is None or stamped is None:
        return "eligible"
    return "eligible" if cur > stamped else "backed_off"


def is_stale_enough(updated_at: "str | None", now: float, *, stall_hours: float) -> "bool | None":
    """``None`` when ``updated_at`` itself doesn't parse — never guess a PR
    is stalled off an unreadable timestamp (fails CLOSED, the opposite
    direction from the back-off check above, because this gate is about to
    grant a NEW work order rather than preserve an old back-off)."""
    at = deploy_reconcile._parse_iso_datetime(updated_at)
    if at is None:
        return None
    return (now - at) / 3600.0 >= stall_hours


# ── viewed-stamp store ───────────────────────────────────────────────────

def default_store_path() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(CONTAINER_DIR, ".aw-workspace")
    return os.path.join(home, VIEWED_STORE_REL)


def load_viewed_store(path: str) -> dict:
    """Fails OPEN on a missing or corrupt file — "one wasted touch beats a
    PR permanently stranded by a read that can't be trusted" (design §2,
    same rule as ``work_loop._backoff_state``)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_viewed_store(path: str, store: dict) -> None:
    """Atomic write (tmp file in the same directory + ``os.replace``) so a
    crash mid-write never leaves a half-written file for the next read to
    choke on."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".work_loop_prs_viewed-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def prune_viewed_store(store: dict, open_pr_keys: "set[str]", *,
                       searched_owners: "set[str] | None" = None) -> dict:
    """Keys no longer in the open-PR set are dropped — keeps the file
    bounded (design §2 "Prune"). A key is only eligible for pruning when
    its OWN owner is in ``searched_owners`` (code-review finding, PR #41):
    pruning unconditionally against whatever `gh search prs` happened to
    return would delete a PR's back-off stamp the moment its owner's
    search call fails (rate limit, timeout) — even though the per-owner
    error isolation this module already has is built to tolerate exactly
    that failure without losing state. ``None`` keeps every key's owner
    implicitly "searched", for callers that already know their input is
    complete (e.g. a direct unit test of pruning alone)."""
    if searched_owners is None:
        return {k: v for k, v in store.items() if k in open_pr_keys}
    return {k: v for k, v in store.items()
           if k in open_pr_keys or k.split("/", 1)[0] not in searched_owners}


# ── the full sweep ────────────────────────────────────────────────────────

def _pr_key(full_name: str, number: int) -> str:
    return f"{full_name}#{number}"


def _build_work_order(candidate: dict, local_path: str) -> dict:
    age = candidate.get("age_hours")
    return {
        "pr": candidate["pr"],
        "url": candidate["url"],
        "reason": f"open PR stalled {age}h, no live or recent CI on HEAD",
        "evidence": (f"updatedAt={candidate.get('updated_at')}; head={candidate.get('head_sha')}; "
                    f"{candidate.get('ci_evidence')}; author={candidate.get('author')}; "
                    f"local checkout repos/{local_path}"),
        "suggested_action": ("push forward: merge if green+approved, comment, dispatch a coder, "
                             "or close if obsolete"),
    }


def _ci_evidence(status_check_rollup: "list | None", now: float) -> str:
    rollup = status_check_rollup or []
    if not rollup:
        return "no CI configured"
    completed_ats = [t for t in
                     (deploy_reconcile._parse_iso_datetime(c.get("completedAt")) for c in rollup)
                     if t is not None]
    if not completed_ats:
        return "no completed checks found"
    newest = max(completed_ats)
    return f"last check completed {round((now - newest) / 3600.0, 1)}h ago"


async def scan_prs(*, now: float, dry_run: bool = False, limit: int = 10,
                   repos_dir: str = REPOS_DIR, gh_runner: GhRunner = default_gh_runner,
                   store_path: "str | None" = None,
                   git_remote_url: "Callable[[str], str | None]" = _git_remote_url,
                   stall_hours: float = DEFAULT_STALL_HOURS,
                   concurrency: int = GH_MAX_CONCURRENCY) -> dict:
    """The real scan, called from the core-side ``POST /pr-scan`` route
    (``routes.py``) — NOT directly injected onto ``WorkLoopContext.scanner``
    any more (see the module docstring for why: that seam is one hop further
    away, in ``mcp_server.py``'s ``_prs_scan_via_route`` HTTP closure to this
    route). The route passes through the caller's own ``now``/``dry_run``/
    ``limit``/``stall_hours`` (``work_loop._prs_loop`` supplies its own
    ``PR_STALL_HOURS`` over the wire); everything else defaults to this
    module's real ``gh``/filesystem/store wiring, and a test replaces the
    whole function rather than faking ``gh_runner`` (see the module
    docstring)."""
    store_path = store_path or default_store_path()

    cloned = await asyncio.to_thread(scan_cloned_repos, repos_dir, git_remote_url=git_remote_url)
    repos_by_full_name = {f"{r['owner']}/{r['repo']}": r for r in cloned["repos"]}
    cloned_full_names = set(repos_by_full_name)

    errors: list = []
    if cloned.get("error"):
        errors.append({"owner": None, "error": cloned["error"]})
    all_prs: list = []
    searched_owners: set = set()
    async def _search(owner: str) -> None:
        rows, error = await search_open_prs_for_owner(owner, gh_runner)
        if error is not None:
            errors.append({"owner": owner, "error": error})
            return
        searched_owners.add(owner)
        all_prs.extend(filter_prs_to_cloned_repos(rows, cloned_full_names))

    await _bounded_gather(cloned["owners"], _search, concurrency=concurrency)

    store = load_viewed_store(store_path)
    open_pr_keys: set = set()
    candidates: list = []
    cheap_survivors: list = []

    for pr in all_prs:
        full_name = (pr.get("repository") or {}).get("nameWithOwner") or ""
        number = pr.get("number")
        key = _pr_key(full_name, number)
        open_pr_keys.add(key)
        updated_at = pr.get("updatedAt")
        is_draft = bool(pr.get("isDraft"))
        backoff = backoff_state(updated_at, store.get(key))
        stale_enough = is_stale_enough(updated_at, now, stall_hours=stall_hours)
        age_hours = None
        at = deploy_reconcile._parse_iso_datetime(updated_at)
        if at is not None:
            age_hours = round((now - at) / 3600.0, 1)

        candidate = {
            "pr": key,
            "url": pr.get("url"),
            "title": pr.get("title"),
            "author": (pr.get("author") or {}).get("login"),
            "is_draft": is_draft,
            "updated_at": updated_at,
            "age_hours": age_hours,
            "backoff": backoff,
            "stalled": None,
        }
        candidates.append(candidate)

        if is_draft:
            candidate["reason"] = "draft — parked by the author"
            continue
        if backoff == "backed_off":
            candidate["reason"] = "backed off — viewed since last activity, waiting for new activity"
            continue
        if stale_enough is None:
            candidate["reason"] = "updatedAt did not parse — skipped rather than guessed"
            continue
        if not stale_enough:
            candidate["stalled"] = False
            candidate["reason"] = f"updated {age_hours}h ago — inside the {stall_hours}h stall window"
            continue
        cheap_survivors.append((candidate, full_name, repos_by_full_name.get(full_name, {}).get("path", "")))

    truncated = len(cheap_survivors) > limit
    probe_batch = cheap_survivors[:limit]
    for candidate, _full_name, _path in cheap_survivors[limit:]:
        candidate["reason"] = "work-order cap reached before this candidate could be probed"

    work_orders: list = []
    tagged: list = []
    forced_reviews: list = []

    async def _probe(item) -> None:
        candidate, full_name, path = item
        view, error = await view_pr(candidate["url"], gh_runner)
        if error is not None:
            candidate["reason"] = f"gh pr view failed: {error}"
            return
        candidate["head_sha"] = view.get("headRefOid")

        # Mechanical force-review check (card 4175bf3b) — independent of the
        # draft/CI gates below, gated ONLY on the devteam:needs-human label
        # and on head_sha having moved since the last stamp this module has
        # for the PR. No prior stamp -> first time seeing it, let the normal
        # round-1 review handle it rather than forcing on day one.
        stamp = store.get(candidate["pr"])
        labels = {(lbl.get("name") or "") for lbl in (view.get("labels") or [])}
        stamped_head_sha = stamp.get("head_sha") if stamp else None
        if (NEEDS_HUMAN_LABEL in labels and stamped_head_sha
                and stamped_head_sha != candidate["head_sha"] and not dry_run):
            comment_error = await post_review_comment(candidate["url"], gh_runner)
            if comment_error is not None:
                errors.append({"owner": None, "error":
                              f"gh pr comment {candidate['url']} failed: {comment_error}"})
            else:
                forced_reviews.append({"pr": candidate["pr"],
                                       "detail": f"head_sha changed {stamped_head_sha} -> "
                                                f"{candidate['head_sha']}"})
                store[candidate["pr"]] = {"viewed_at": now, "updated_at": candidate["updated_at"],
                                          "head_sha": candidate["head_sha"]}

        if view.get("isDraft"):
            candidate["is_draft"] = True
            candidate["reason"] = "draft — parked by the author (confirmed at probe time)"
            return
        rollup = view.get("statusCheckRollup")
        candidate["ci_evidence"] = _ci_evidence(rollup, now)
        no_ci = no_live_or_recent_ci(rollup, now, stall_hours=stall_hours)
        candidate["stalled"] = no_ci
        if not no_ci:
            candidate["reason"] = "live or recently-completed CI on HEAD"
            return
        work_orders.append(_build_work_order(candidate, path))
        if not dry_run:
            store[candidate["pr"]] = {"viewed_at": now, "updated_at": candidate["updated_at"],
                                      "head_sha": candidate["head_sha"]}
            tagged.append({"pr": candidate["pr"], "detail": f"updated_at={candidate['updated_at']}"})

    await _bounded_gather(probe_batch, _probe, concurrency=concurrency)

    if not dry_run:
        pruned = prune_viewed_store(store, open_pr_keys, searched_owners=searched_owners)
        save_viewed_store(store_path, pruned)

    return {
        "repos_unmapped": cloned["unmapped"],
        "owners": cloned["owners"],
        "candidates": candidates,
        "counts": {"owners": len(cloned["owners"]), "candidates_considered": len(all_prs),
                  "probed": len(probe_batch), "stalled": len(work_orders)},
        "truncated": truncated,
        "errors": errors,
        "work_orders": work_orders,
        "tagged": tagged,
        "forced_reviews": forced_reviews,
    }


__all__ = ["GhRunner", "default_gh_runner", "parse_github_remote", "map_cloned_repos",
           "scan_cloned_repos", "search_open_prs_for_owner", "view_pr", "post_review_comment",
           "filter_prs_to_cloned_repos", "no_live_or_recent_ci", "backoff_state",
           "is_stale_enough", "load_viewed_store", "save_viewed_store", "prune_viewed_store",
           "default_store_path", "scan_prs", "DEFAULT_STALL_HOURS", "REPOS_DIR", "SEARCH_LIMIT",
           "NEEDS_HUMAN_LABEL"]
