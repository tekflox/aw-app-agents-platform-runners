"""Backfill tool for Kanban observability:execution-index-skipped-other-
unclassified — re-index runs that were silently dropped by the
httpx.ReadTimeout bug fixed in execution_index.py (the shared client's
timeout=5 was too short for aw-app-kb's POST /api/kb/executions, which runs
synchronous embedding generation server-side before the INSERT).

Safe to run repeatedly: POST /api/kb/executions upserts by run_id
(aw-app-kb's exec_pg.upsert_chunks — re-indexing replaces, never
duplicates), and index_run() re-applies the same _interesting() gate it
always has, so a run that was correctly excluded stays excluded here too.

NOT wired into this app's own routes/CLI — this is a one-off operational
tool for whoever runs the backfill (see the Kanban card), not something
the app calls itself. It imports execution_index directly and drives it
against the real agents-platform-multitenant + aw-app-kb endpoints using
this app's own live config (same one execution_index.configure() receives
at runtime) — run it from wherever that config and network access are
both reachable.

Usage:
    python3 scripts/backfill_execution_index.py --config /path/to/config.json \
        [--limit 500] [--dry-run]

``--config`` must point at a JSON file shaped like this app's own
app-config (agents_platform_base, agents_platform_token, kb_base_url,
execution_index_secret, execution_index_mode) — see
.aw-workspace/app-config/agents-platform-runners.json in an aw-workspace
deployment.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from agents_platform_runners_app import execution_index as idx  # noqa: E402

TERMINAL = {"success", "error", "cancelled"}


def _fetch_run_ids(cfg: dict, limit: int) -> list[str]:
    """Every run this tenant's own token can see that has finished — AP-MT's
    /api/runs is tenant-scoped server-side (one tenant per aw-workspace), so
    this never reaches into another workspace's runs."""
    base = str(cfg["agents_platform_base"]).rstrip("/")
    headers = {"Authorization": f"Bearer {cfg['agents_platform_token']}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(f"{base}/api/runs", params={"limit": str(limit), "summary": "true"})
        resp.raise_for_status()
        runs = resp.json()
    return [r["id"] for r in runs if r.get("status") in TERMINAL and r.get("ended_at")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to a JSON file shaped like this app's app-config")
    parser.add_argument("--limit", type=int, default=500, help="how many recent ended runs to consider (AP-MT max 5000)")
    parser.add_argument("--dry-run", action="store_true", help="list the candidate run_ids without indexing them")
    args = parser.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    idx.configure(cfg)

    run_ids = _fetch_run_ids(cfg, args.limit)
    print(f"candidates: {len(run_ids)} terminal runs with ended_at set", file=sys.stderr)
    if args.dry_run:
        for run_id in run_ids:
            print(run_id)
        return

    indexed = not_indexed = 0
    for i, run_id in enumerate(run_ids, start=1):
        t0 = time.monotonic()
        ok = idx.index_run(run_id, config=cfg)
        dt = time.monotonic() - t0
        if ok:
            indexed += 1
        else:
            # index_run() doesn't distinguish "gated" from "skipped" in its
            # return value — both are False. The Redis counters (see
            # /execution-index/status) are the authoritative breakdown.
            not_indexed += 1
        print(f"[{i}/{len(run_ids)}] run_id={run_id} indexed={ok} elapsed={dt:.1f}s", file=sys.stderr)
    print(f"done: indexed={indexed} not_indexed={not_indexed}", file=sys.stderr)


if __name__ == "__main__":
    main()
