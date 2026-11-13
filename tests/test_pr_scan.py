"""``pr_scan`` — the work_loop `prs` loop's internals: remote parsing, the
two-phase gh sweep, the staleness/CI decision functions, and the per-PR
viewed-stamp store.

Same split as test_work_loop.py / test_deploy_reconcile.py: pure decision
logic plus a fake ``gh`` runner, no real subprocess/network calls. The one
thing this file does touch on disk is the viewed-stamp store, always via a
pytest ``tmp_path`` — never the real ``AW_WORKSPACE_HOME`` location.

Run: python -m pytest tests/test_pr_scan.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import pr_scan as ps  # noqa: E402

NOW = 1_800_000_000.0  # arbitrary fixed epoch — no wall-clock dependency


def _iso(hours_ago: float) -> str:
    ts = dt.datetime.fromtimestamp(NOW - hours_ago * 3600, tz=dt.timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


# ── remote parsing ───────────────────────────────────────────────────────

def test_parses_ssh_colon_form():
    assert ps.parse_github_remote("git@github.com:tekflox/aw-app-kb.git") == ("tekflox", "aw-app-kb")


def test_parses_ssh_colon_form_without_dot_git():
    assert ps.parse_github_remote("git@github.com:tekflox/aw-app-kb") == ("tekflox", "aw-app-kb")


def test_parses_ssh_url_form():
    assert ps.parse_github_remote("ssh://git@github.com/tekflox/aw-automation.git") == (
        "tekflox", "aw-automation")


def test_parses_https_form():
    assert ps.parse_github_remote("https://github.com/tekflox/aw-app-kb.git") == (
        "tekflox", "aw-app-kb")


def test_parses_https_form_without_dot_git():
    assert ps.parse_github_remote("https://github.com/fredericowu/agentic-workspace") == (
        "fredericowu", "agentic-workspace")


def test_parses_https_form_with_embedded_credentials():
    assert ps.parse_github_remote("https://x-access-token:tok@github.com/tekflox/aw-app-kb.git") == (
        "tekflox", "aw-app-kb")


def test_rejects_a_non_github_remote():
    assert ps.parse_github_remote("https://gitlab.com/someone/somewhere.git") is None


def test_rejects_empty_or_none():
    assert ps.parse_github_remote("") is None
    assert ps.parse_github_remote(None) is None


# ── map_cloned_repos ─────────────────────────────────────────────────────

def test_map_cloned_repos_dedups_two_dirs_cloning_the_same_github_repo():
    result = ps.map_cloned_repos({
        "aw-app-kb": "git@github.com:tekflox/aw-app-kb.git",
        "aw-app-kb-copy": "https://github.com/tekflox/aw-app-kb.git",
    })
    assert len(result["repos"]) == 1
    assert result["owners"] == ["tekflox"]
    assert result["unmapped"] == []


def test_map_cloned_repos_reports_unmapped_dirs_never_drops_them():
    result = ps.map_cloned_repos({
        "has-remote": "git@github.com:tekflox/aw-app-kb.git",
        "no-remote": None,
        "non-github": "https://gitlab.com/x/y.git",
    })
    assert result["unmapped"] == ["no-remote", "non-github"]
    assert [r["path"] for r in result["repos"]] == ["has-remote"]


# ── scan_cloned_repos (filesystem + injected git_remote_url) ────────────

def test_scan_cloned_repos_only_considers_dirs_with_a_dot_git(tmp_path):
    (tmp_path / "real-repo" / ".git").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()

    def fake_remote(path):
        return "git@github.com:tekflox/real-repo.git" if path.endswith("real-repo") else None

    result = ps.scan_cloned_repos(str(tmp_path), git_remote_url=fake_remote)
    assert [r["path"] for r in result["repos"]] == ["real-repo"]
    assert result["unmapped"] == []


def test_scan_cloned_repos_carries_a_missing_repos_dir_as_an_error_not_a_clean_empty(tmp_path):
    """Code-review finding (gateway-boundary follow-up, card 4175bf3b): a
    bare {"repos": [], "owners": [], "unmapped": []} here is
    indistinguishable from "this workspace genuinely has zero cloned
    repos" — exactly the shape that let the prs loop report ok:true with
    nothing to show for it, with no error anywhere to say why, for a full
    day in production."""
    missing = str(tmp_path / "does-not-exist")
    result = ps.scan_cloned_repos(missing)
    assert result["repos"] == [] and result["owners"] == [] and result["unmapped"] == []
    assert missing in result["error"]


# ── filter_prs_to_cloned_repos ───────────────────────────────────────────

def test_filter_keeps_only_cloned_repos():
    prs = [
        {"number": 1, "repository": {"nameWithOwner": "tekflox/aw-app-kb"}},
        {"number": 2, "repository": {"nameWithOwner": "tekflox/some-other-repo"}},
    ]
    kept = ps.filter_prs_to_cloned_repos(prs, {"tekflox/aw-app-kb"})
    assert [p["number"] for p in kept] == [1]


def test_search_sorts_oldest_updated_first_with_a_high_limit():
    """Code-review finding (PR #41): `gh search prs` defaults to 30 results
    sorted by best-match relevance, not updatedAt — an owner with more open
    PRs than that could silently lose exactly the most-neglected ones.
    Sorting updated/asc means a PR cut off by any limit is, by
    construction, one of the LEAST stale ones."""
    captured = {}

    async def runner(args):
        captured["args"] = args
        return 0, "[]", ""

    asyncio.run(ps.search_open_prs_for_owner("tekflox", runner))
    args = captured["args"]
    assert args[args.index("--sort") + 1] == "updated"
    assert args[args.index("--order") + 1] == "asc"
    assert int(args[args.index("--limit") + 1]) >= 1000


# ── no_live_or_recent_ci — staleness/CI matrix ──────────────────────────

def test_no_rollup_at_all_passes_as_no_ci():
    assert ps.no_live_or_recent_ci(None, NOW, stall_hours=6) is True
    assert ps.no_live_or_recent_ci([], NOW, stall_hours=6) is True


def test_in_progress_status_is_live_even_with_success_conclusion_elsewhere():
    rollup = [{"status": "IN_PROGRESS", "conclusion": None, "completedAt": None}]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is False


def test_queued_and_pending_are_also_live():
    assert ps.no_live_or_recent_ci([{"status": "QUEUED"}], NOW, stall_hours=6) is False
    assert ps.no_live_or_recent_ci([{"status": "PENDING"}], NOW, stall_hours=6) is False


def test_skipped_conclusion_is_completed_status_not_live_despite_the_name():
    """The exact gotcha the design flags (§7, risk #2): a SKIPPED check is
    status=COMPLETED, never "in progress" — a conclusion-based read would
    misclassify this."""
    rollup = [{"status": "COMPLETED", "conclusion": "SKIPPED", "completedAt": _iso(10)}]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is True


def test_a_recently_completed_check_is_not_stale_yet():
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": _iso(2)}]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is False


def test_an_old_completed_check_is_stale():
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": _iso(10)}]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is True


def test_the_newest_completed_check_wins_when_mixed_ages():
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": _iso(10)},
        {"status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": _iso(1)},
    ]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is False


def test_a_re_run_that_does_not_bump_updated_at_is_still_caught_here():
    """This is the whole reason the CI gate is separate from the updatedAt
    gate — a check re-run does not bump the PR's own updatedAt."""
    rollup = [{"status": "IN_PROGRESS"}]
    assert ps.no_live_or_recent_ci(rollup, NOW, stall_hours=6) is False


# ── backoff_state ─────────────────────────────────────────────────────────

def test_backoff_eligible_with_no_stamp_at_all():
    assert ps.backoff_state(_iso(1), None) == "eligible"


def test_backoff_backed_off_when_pr_has_not_moved_since_the_stamp():
    stamp = {"updated_at": _iso(1), "viewed_at": NOW - 3600}
    assert ps.backoff_state(_iso(1), stamp) == "backed_off"


def test_backoff_re_arms_when_current_updated_at_is_newer_than_the_stamp():
    stamp = {"updated_at": _iso(5), "viewed_at": NOW - 3600}
    assert ps.backoff_state(_iso(1), stamp) == "eligible"  # newer activity since the stamp


def test_backoff_fails_open_on_an_unparseable_stamped_timestamp():
    stamp = {"updated_at": "not-a-date", "viewed_at": NOW}
    assert ps.backoff_state(_iso(1), stamp) == "eligible"


def test_backoff_fails_open_on_an_unparseable_current_timestamp():
    stamp = {"updated_at": _iso(1), "viewed_at": NOW}
    assert ps.backoff_state("not-a-date", stamp) == "eligible"


# ── is_stale_enough ───────────────────────────────────────────────────────

def test_is_stale_enough_true_past_the_window():
    assert ps.is_stale_enough(_iso(10), NOW, stall_hours=6) is True


def test_is_stale_enough_false_inside_the_window():
    assert ps.is_stale_enough(_iso(1), NOW, stall_hours=6) is False


def test_is_stale_enough_none_on_unparseable_timestamp():
    assert ps.is_stale_enough("garbage", NOW, stall_hours=6) is None
    assert ps.is_stale_enough(None, NOW, stall_hours=6) is None


# ── viewed store: round-trip / prune / atomicity / fail-open ────────────

def test_store_round_trips(tmp_path):
    path = str(tmp_path / "viewed.json")
    store = {"tekflox/aw-app-kb#7": {"viewed_at": NOW, "updated_at": _iso(10), "head_sha": "abc"}}
    ps.save_viewed_store(path, store)
    assert ps.load_viewed_store(path) == store


def test_store_write_is_atomic_no_tmp_file_left_behind(tmp_path):
    path = str(tmp_path / "viewed.json")
    ps.save_viewed_store(path, {"k": {"v": 1}})
    leftovers = [p for p in tmp_path.iterdir() if p.name != "viewed.json"]
    assert leftovers == []


def test_store_prune_drops_keys_no_longer_open():
    store = {"a#1": {"updated_at": "x"}, "b#2": {"updated_at": "y"}}
    pruned = ps.prune_viewed_store(store, {"a#1"})
    assert pruned == {"a#1": {"updated_at": "x"}}


def test_store_load_fails_open_on_a_missing_file(tmp_path):
    assert ps.load_viewed_store(str(tmp_path / "nope.json")) == {}


def test_store_load_fails_open_on_a_corrupt_file(tmp_path):
    path = tmp_path / "viewed.json"
    path.write_text("{not valid json")
    assert ps.load_viewed_store(str(path)) == {}


def test_store_load_fails_open_on_a_non_dict_json_body(tmp_path):
    path = tmp_path / "viewed.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert ps.load_viewed_store(str(path)) == {}


# ── scan_prs — the full sweep, fake gh_runner, no network ────────────────

def _fake_runner(search_responses=None, view_responses=None, search_errors=None):
    """``search_responses``: ``{owner: [pr_dict, ...]}``. ``view_responses``:
    ``{url: view_dict}``. Returns ``(code, stdout, stderr)`` triples the way
    a real subprocess would."""
    search_responses = search_responses or {}
    view_responses = view_responses or {}
    search_errors = search_errors or {}

    async def runner(args):
        if args[:2] == ["search", "prs"]:
            owner = args[args.index("--owner") + 1]
            if owner in search_errors:
                return 1, "", search_errors[owner]
            return 0, json.dumps(search_responses.get(owner, [])), ""
        if args[:2] == ["pr", "view"]:
            url = args[2]
            return 0, json.dumps(view_responses.get(url, {})), ""
        raise AssertionError(f"unexpected gh invocation: {args}")

    return runner


def _search_pr(number, *, owner="tekflox", repo="aw-app-kb", updated_hours_ago=10,
              is_draft=False, title="some PR"):
    return {"number": number, "repository": {"name": repo, "nameWithOwner": f"{owner}/{repo}"},
            "title": title, "url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "updatedAt": _iso(updated_hours_ago), "isDraft": is_draft,
            "createdAt": _iso(updated_hours_ago + 1), "author": {"login": "fredericowu"}}


def _scan(tmp_path, *, search_responses, view_responses=None, search_errors=None,
          dry_run=False, limit=10, stall_hours=6, store=None):
    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)

    def git_remote_url(path):
        return "git@github.com:tekflox/aw-app-kb.git"

    store_path = str(tmp_path / "viewed.json")
    if store is not None:
        ps.save_viewed_store(store_path, store)

    runner = _fake_runner(search_responses=search_responses, view_responses=view_responses,
                          search_errors=search_errors)
    return asyncio.run(ps.scan_prs(
        now=NOW, dry_run=dry_run, limit=limit, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=git_remote_url, stall_hours=stall_hours)), store_path


def test_a_stalled_pr_becomes_one_work_order_and_gets_tagged(tmp_path):
    pr = _search_pr(7, updated_hours_ago=72)
    view = {"headRefOid": "e2cb328", "isDraft": False,
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    result, store_path = _scan(tmp_path, search_responses={"tekflox": [pr]},
                               view_responses={pr["url"]: view})
    assert result["work_orders"] == [{
        "pr": "tekflox/aw-app-kb#7", "url": pr["url"],
        "reason": "open PR stalled 72.0h, no live or recent CI on HEAD",
        "evidence": (f"updatedAt={pr['updatedAt']}; head=e2cb328; last check completed 50.0h ago; "
                    "author=fredericowu; local checkout repos/aw-app-kb"),
        "suggested_action": ("push forward: merge if green+approved, comment, dispatch a coder, "
                             "or close if obsolete"),
    }]
    assert result["tagged"] == [{"pr": "tekflox/aw-app-kb#7", "detail": f"updated_at={pr['updatedAt']}"}]
    stored = ps.load_viewed_store(store_path)
    assert stored["tekflox/aw-app-kb#7"]["head_sha"] == "e2cb328"


def test_a_draft_pr_never_becomes_a_work_order(tmp_path):
    pr = _search_pr(8, updated_hours_ago=72, is_draft=True)
    result, _ = _scan(tmp_path, search_responses={"tekflox": [pr]})
    assert result["work_orders"] == []
    candidate = result["candidates"][0]
    assert candidate["is_draft"] is True
    assert candidate["stalled"] is None


def test_a_fresh_pr_is_not_probed_at_all(tmp_path):
    pr = _search_pr(9, updated_hours_ago=1)
    result, _ = _scan(tmp_path, search_responses={"tekflox": [pr]}, view_responses={})
    candidate = result["candidates"][0]
    assert candidate["stalled"] is False
    assert result["counts"]["probed"] == 0


def test_live_ci_on_head_prevents_the_work_order(tmp_path):
    pr = _search_pr(10, updated_hours_ago=72)
    view = {"headRefOid": "abc123", "isDraft": False,
           "statusCheckRollup": [{"status": "IN_PROGRESS"}]}
    result, _ = _scan(tmp_path, search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view})
    assert result["work_orders"] == []
    assert result["candidates"][0]["stalled"] is False


def test_already_backed_off_pr_is_not_reprobed(tmp_path):
    pr = _search_pr(11, updated_hours_ago=72)
    stamp = {"tekflox/aw-app-kb#11": {"viewed_at": NOW - 3600, "updated_at": pr["updatedAt"],
                                      "head_sha": "old"}}
    result, _ = _scan(tmp_path, search_responses={"tekflox": [pr]}, view_responses={}, store=stamp)
    assert result["candidates"][0]["backoff"] == "backed_off"
    assert result["work_orders"] == []


def test_new_activity_since_the_stamp_re_arms_eligibility(tmp_path):
    pr = _search_pr(12, updated_hours_ago=72)
    older_stamp_time = _iso(100)  # stamped before this updatedAt
    stamp = {"tekflox/aw-app-kb#12": {"viewed_at": NOW - 3600, "updated_at": older_stamp_time,
                                      "head_sha": "old"}}
    view = {"headRefOid": "new-sha", "isDraft": False,
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    result, _ = _scan(tmp_path, search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view},
                      store=stamp)
    assert result["candidates"][0]["backoff"] == "eligible"
    assert len(result["work_orders"]) == 1


def test_dry_run_writes_nothing_to_the_store(tmp_path):
    pr = _search_pr(13, updated_hours_ago=72)
    view = {"headRefOid": "sha1", "isDraft": False,
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    result, store_path = _scan(tmp_path, search_responses={"tekflox": [pr]},
                               view_responses={pr["url"]: view}, dry_run=True)
    assert len(result["work_orders"]) == 1  # still reported
    assert result["tagged"] == []  # but nothing tagged
    assert not Path(store_path).exists()  # and nothing written at all


def test_cap_is_applied_before_phase_b_probing_and_reports_truncated(tmp_path):
    prs = [_search_pr(n, updated_hours_ago=72) for n in (14, 15, 16)]
    probed_urls = []

    def tracking_view_runner(search_responses, view_responses):
        async def runner(args):
            if args[:2] == ["search", "prs"]:
                return 0, json.dumps(search_responses), ""
            if args[:2] == ["pr", "view"]:
                probed_urls.append(args[2])
                return 0, json.dumps(view_responses.get(args[2], {
                    "headRefOid": "x", "isDraft": False,
                    "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                           "completedAt": _iso(50)}]})), ""
            raise AssertionError(args)
        return runner

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=False, limit=2, repos_dir=str(repos_dir),
        gh_runner=tracking_view_runner(prs, {}),
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))
    assert result["truncated"] is True
    assert len(probed_urls) == 2
    assert result["counts"]["probed"] == 2


def test_scan_prs_surfaces_an_unreadable_repos_dir_as_an_error_not_a_clean_empty(tmp_path):
    """Same finding as scan_cloned_repos's own unit test, exercised through
    the full ``scan_prs`` entry point — this is the shape ``work_loop.py``'s
    ``_prs_loop`` actually sees (``section["errors"]``), the one that used
    to come back empty with no error at all."""
    missing = str(tmp_path / "does-not-exist")

    async def exploding_runner(args):
        raise AssertionError("no owners were ever found — gh must never be called")

    result = asyncio.run(ps.scan_prs(now=NOW, dry_run=False, limit=10, repos_dir=missing,
                                     gh_runner=exploding_runner,
                                     store_path=str(tmp_path / "viewed.json")))
    assert result["owners"] == []
    assert result["candidates"] == []
    assert len(result["errors"]) == 1
    assert missing in result["errors"][0]["error"]
    assert result["errors"][0]["owner"] is None


def test_per_owner_search_failure_is_isolated_and_other_owners_continue(tmp_path):
    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    (repos_dir / "other-repo" / ".git").mkdir(parents=True)

    def git_remote_url(path):
        if path.endswith("aw-app-kb"):
            return "git@github.com:tekflox/aw-app-kb.git"
        return "git@github.com:someone-else/other-repo.git"

    pr = _search_pr(17, updated_hours_ago=72, owner="someone-else", repo="other-repo")
    view = {"headRefOid": "sha", "isDraft": False,
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    store_path = str(tmp_path / "viewed.json")
    # tekflox/aw-app-kb#99 was viewed by a PRIOR pass and is still genuinely
    # open — but THIS pass's tekflox search fails, so scan_prs never learns
    # that from gh. Pruning must not treat "not returned by a failed search"
    # as "closed".
    ps.save_viewed_store(store_path, {
        "tekflox/aw-app-kb#99": {"viewed_at": NOW - 7200, "updated_at": _iso(100), "head_sha": "z"}})
    runner = _fake_runner(search_responses={"someone-else": [pr]}, view_responses={pr["url"]: view},
                          search_errors={"tekflox": "gh: rate limited"})
    result = asyncio.run(ps.scan_prs(now=NOW, dry_run=False, limit=10, repos_dir=str(repos_dir),
                                     gh_runner=runner, store_path=store_path,
                                     git_remote_url=git_remote_url, stall_hours=6))
    assert result["errors"] == [{"owner": "tekflox", "error": "gh: rate limited"}]
    assert len(result["work_orders"]) == 1  # someone-else's owner search still succeeded
    stored = ps.load_viewed_store(store_path)
    assert "tekflox/aw-app-kb#99" in stored, (
        "code-review finding, PR #41: a failed owner search must not be read as "
        "'this owner now has zero open PRs' and wipe that owner's back-off state")


def test_post_review_comment_posts_aw_review_and_returns_none_on_success():
    captured = {}

    async def runner(args):
        captured["args"] = args
        return 0, "", ""

    error = asyncio.run(ps.post_review_comment("https://github.com/tekflox/aw-app-kb/pull/7", runner))
    assert error is None
    assert captured["args"] == ["pr", "comment", "https://github.com/tekflox/aw-app-kb/pull/7",
                                "--body", "/aw-review"]


def test_post_review_comment_returns_the_error_on_failure():
    async def runner(args):
        return 1, "", "gh: not authenticated"

    error = asyncio.run(ps.post_review_comment("https://github.com/tekflox/aw-app-kb/pull/7", runner))
    assert error == "gh: not authenticated"


# ── force-review on a needs-human PR with a new head_sha (card 4175bf3b) ──

def _fake_runner_with_comments(search_responses=None, view_responses=None, comment_urls=None):
    """Same as ``_fake_runner`` but also handles ``gh pr comment`` calls,
    recording each commented URL into ``comment_urls`` (a list the caller
    passes in so it can inspect it after the run)."""
    search_responses = search_responses or {}
    view_responses = view_responses or {}
    comment_urls = comment_urls if comment_urls is not None else []

    async def runner(args):
        if args[:2] == ["search", "prs"]:
            owner = args[args.index("--owner") + 1]
            return 0, json.dumps(search_responses.get(owner, [])), ""
        if args[:2] == ["pr", "view"]:
            url = args[2]
            return 0, json.dumps(view_responses.get(url, {})), ""
        if args[:2] == ["pr", "comment"]:
            comment_urls.append(args[2])
            return 0, "", ""
        raise AssertionError(f"unexpected gh invocation: {args}")

    return runner


def test_needs_human_pr_with_a_new_head_sha_gets_a_forced_review(tmp_path):
    pr = _search_pr(20, updated_hours_ago=72)
    view = {"headRefOid": "new-sha", "isDraft": False,
           "labels": [{"name": "devteam:needs-human"}],
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    # Stamped BEFORE the PR's current updatedAt (the push that moved
    # head_sha also bumps updatedAt) -> backoff re-arms to "eligible",
    # same construction as test_new_activity_since_the_stamp_re_arms_eligibility.
    stamp = {"tekflox/aw-app-kb#20": {"viewed_at": NOW - 7200, "updated_at": _iso(100),
                                      "head_sha": "old-sha"}}

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    ps.save_viewed_store(store_path, stamp)
    comment_urls = []
    runner = _fake_runner_with_comments(search_responses={"tekflox": [pr]},
                                        view_responses={pr["url"]: view}, comment_urls=comment_urls)

    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=False, limit=10, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))

    assert comment_urls == [pr["url"]]
    assert result["forced_reviews"] == [
        {"pr": "tekflox/aw-app-kb#20", "detail": "head_sha changed old-sha -> new-sha"}]


def test_needs_human_pr_with_the_same_head_sha_is_not_re_reviewed(tmp_path):
    """Dedup: the same head_sha the store already has for this PR must
    never be force-reviewed twice."""
    pr = _search_pr(21, updated_hours_ago=72)
    view = {"headRefOid": "same-sha", "isDraft": False,
           "labels": [{"name": "devteam:needs-human"}],
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    stamp = {"tekflox/aw-app-kb#21": {"viewed_at": NOW - 7200, "updated_at": _iso(100),
                                      "head_sha": "same-sha"}}

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    ps.save_viewed_store(store_path, stamp)
    comment_urls = []
    runner = _fake_runner_with_comments(search_responses={"tekflox": [pr]},
                                        view_responses={pr["url"]: view}, comment_urls=comment_urls)

    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=False, limit=10, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))

    assert comment_urls == []
    assert result["forced_reviews"] == []


def test_pr_without_the_needs_human_label_is_never_force_reviewed(tmp_path):
    pr = _search_pr(22, updated_hours_ago=72)
    view = {"headRefOid": "new-sha", "isDraft": False, "labels": [],
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    stamp = {"tekflox/aw-app-kb#22": {"viewed_at": NOW - 7200, "updated_at": _iso(100),
                                      "head_sha": "old-sha"}}

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    ps.save_viewed_store(store_path, stamp)
    comment_urls = []
    runner = _fake_runner_with_comments(search_responses={"tekflox": [pr]},
                                        view_responses={pr["url"]: view}, comment_urls=comment_urls)

    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=False, limit=10, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))

    assert comment_urls == []
    assert result["forced_reviews"] == []


def test_needs_human_pr_with_no_prior_stamp_is_not_force_reviewed_on_day_one(tmp_path):
    """First time scan_prs ever sees this PR -> no stamp yet -> let the
    normal round-1 review handle it, don't force on day one."""
    pr = _search_pr(23, updated_hours_ago=72)
    view = {"headRefOid": "first-sha", "isDraft": False,
           "labels": [{"name": "devteam:needs-human"}],
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    comment_urls = []
    runner = _fake_runner_with_comments(search_responses={"tekflox": [pr]},
                                        view_responses={pr["url"]: view}, comment_urls=comment_urls)

    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=False, limit=10, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))

    assert comment_urls == []
    assert result["forced_reviews"] == []


def test_dry_run_never_posts_a_forced_review_comment(tmp_path):
    pr = _search_pr(24, updated_hours_ago=72)
    view = {"headRefOid": "new-sha", "isDraft": False,
           "labels": [{"name": "devteam:needs-human"}],
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    stamp = {"tekflox/aw-app-kb#24": {"viewed_at": NOW - 7200, "updated_at": _iso(100),
                                      "head_sha": "old-sha"}}

    repos_dir = tmp_path / "repos"
    (repos_dir / "aw-app-kb" / ".git").mkdir(parents=True)
    store_path = str(tmp_path / "viewed.json")
    ps.save_viewed_store(store_path, stamp)
    comment_urls = []
    runner = _fake_runner_with_comments(search_responses={"tekflox": [pr]},
                                        view_responses={pr["url"]: view}, comment_urls=comment_urls)

    result = asyncio.run(ps.scan_prs(
        now=NOW, dry_run=True, limit=10, repos_dir=str(repos_dir), gh_runner=runner,
        store_path=store_path, git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        stall_hours=6))

    assert comment_urls == []
    assert result["forced_reviews"] == []


def test_prune_viewed_store_only_drops_keys_whose_owner_was_actually_searched():
    store = {"a/x#1": {"updated_at": "t"}, "b/y#2": {"updated_at": "t"}}
    pruned = ps.prune_viewed_store(store, set(), searched_owners={"a"})
    assert pruned == {"b/y#2": {"updated_at": "t"}}, (
        "a#1's owner WAS searched and is no longer open -> pruned; "
        "b#2's owner was NOT searched this pass -> left untouched")


def test_pruning_drops_a_pr_that_is_no_longer_open(tmp_path):
    pr = _search_pr(18, updated_hours_ago=72)
    stamp = {"tekflox/aw-app-kb#18": {"viewed_at": NOW - 7200, "updated_at": pr["updatedAt"],
                                      "head_sha": "x"},
            "tekflox/aw-app-kb#999": {"viewed_at": NOW - 100000, "updated_at": _iso(200),
                                      "head_sha": "y"}}
    view = {"headRefOid": "x", "isDraft": False,
           "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS",
                                  "completedAt": _iso(50)}]}
    _, store_path = _scan(tmp_path, search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view},
                          store=stamp)
    stored = ps.load_viewed_store(store_path)
    assert "tekflox/aw-app-kb#999" not in stored  # pruned — no longer among the open PRs
