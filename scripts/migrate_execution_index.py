"""Move the ``executions`` table out of aw-app-kb's bundled Postgres into the
shared ``aw-remote-host-postgres``, and prove the move was exact.

Design: ``docs/design/execution-index-migration-off-aw-app-kb.md`` §3.2
(the data move) and §3.3 step 4 (the parity check). Operational tool, not
something the app calls itself — same posture as
``backfill_execution_index.py`` next to it.

Three subcommands:

``copy``
    Export every row from the source and import it into the destination.
    **COPY, not ``pg_dump``** — the source is PostgreSQL 17 and the
    destination is 16, and restoring a newer major's dump into an older one
    is the unsupported direction. The ``vector`` type round-trips through
    its own text representation (``embedding::text``), which is exactly what
    makes the version hop a non-event; and because that text is pgvector's
    shortest-roundtrip float output, the re-parsed values are bit-identical,
    which is what the ``parity`` check below is able to assert.

    Lands in a **staging table** first, then
    ``INSERT … ON CONFLICT (chunk_id) DO NOTHING`` into ``executions`` —
    never COPY straight into the live table, which aborts on the first
    dual-written row's primary key. DO NOTHING rather than DO UPDATE because
    by the time this runs, dual-write is live (cutover step 2) and any
    conflicting destination row is NEWER than the export.

    Idempotent and re-runnable, which is what makes "run it again after the
    producer's dual-write actually goes live" a free way to close any gap.

``counts``
    Chunk/run counts and the ``indexed_at`` range on both sides.

``parity``
    The check that matters. Same model and the same vectors must give the
    same scores, so for a set of queries it compares the *ranked chunk ids
    and their cosine scores* from both stores. A drift is not noise to be
    tuned away with a threshold: it means the embedding path diverged —
    a wrong task prefix, a different truncation bound, a different fastembed
    revision — which produces *working* search with silently worse recall
    and no error anywhere (design §6). Reported, never smoothed.

    The source scores come from aw-app-kb's own HTTP search (so its real
    embedder runs), the destination scores from the exec-index service's
    ``/search``. Comparing the two HTTP surfaces rather than two libraries is
    deliberate: it exercises the embedder each side will actually use.

Usage (run inside the workspace container, which is the only place that can
reach both the source container and the destination Postgres)::

    python3 scripts/migrate_execution_index.py counts
    python3 scripts/migrate_execution_index.py copy [--batch 2000] [--dry-run]
    python3 scripts/migrate_execution_index.py parity [--queries-file q.txt]

Connection sources, so no credential is ever passed on a command line:

* destination — ``exec_index_store.get_pg_url()``, i.e. derived from the
  workspace's own ``AW_WORKSPACE_DB_URL``.
* source — ``KB_SOURCE_PG_URL``, or the default
  ``postgresql://postgres:postgres@aw-app-kb:5432/knowledge_base``
  (aw-app-kb's bundled Postgres, whose credentials are its image defaults
  and are not a secret).
* HTTP — ``KB_BASE_URL`` / ``EXEC_INDEX_URL``, plus
  ``EXEC_INDEX_SECRET``/``KB_EXEC_SECRET`` only if a write is needed (none
  of these subcommands write over HTTP).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import exec_index_store as store  # noqa: E402

DEFAULT_SOURCE_PG_URL = "postgresql://postgres:postgres@aw-app-kb:5432/knowledge_base"
DEFAULT_KB_BASE_URL = "http://aw-app-kb:8000"
DEFAULT_EXEC_INDEX_URL = "http://127.0.0.1:9408"

#: Staging table name. Dropped and recreated by every ``copy`` run — it is
#: scratch, and a leftover from an aborted run must never be silently
#: re-imported as if it were this run's export.
STAGING = "executions_import_staging"

#: The columns that move, in order. ``embedding`` is cast to text on the way
#: out and back to ``vector`` on the way in; everything else is native.
COLUMNS = ("chunk_id", "run_id", "seq", "content", "metadata", "indexed_at")

#: Default parity queries. The first is the known-good one from
#: ``docs/design/apmt-execution-history-search.md`` the design names; the
#: rest spread over different chunk kinds so a prefix/truncation divergence
#: has more than one chance to show up.
DEFAULT_QUERIES = (
    "fs-sync delete policy approve pending deletions",
    "have we seen this tool error before and how did it resolve",
    "postgres connection refused from an app container",
    "kanban card moved to need human by a blocker",
    "warm container drained and respawned",
)


def source_pg_url() -> str:
    return os.environ.get("KB_SOURCE_PG_URL", "").strip() or DEFAULT_SOURCE_PG_URL


def kb_base_url() -> str:
    return (os.environ.get("KB_BASE_URL", "").strip()
            or DEFAULT_KB_BASE_URL).rstrip("/")


def exec_index_url() -> str:
    return (os.environ.get("EXEC_INDEX_URL", "").strip()
            or DEFAULT_EXEC_INDEX_URL).rstrip("/")


# ---------------------------------------------------------------------------
# counts
# ---------------------------------------------------------------------------

_COUNT_SQL = ("SELECT COUNT(*), COUNT(DISTINCT run_id), MIN(indexed_at), MAX(indexed_at) "
              "FROM executions")


def _counts(url: str) -> dict:
    import psycopg
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(_COUNT_SQL).fetchone() or (0, 0, None, None)
        return {"chunk_count": row[0], "run_count": row[1],
                "oldest_indexed_at": str(row[2]) if row[2] else None,
                "newest_indexed_at": str(row[3]) if row[3] else None}
    except Exception as exc:  # noqa: BLE001 — an error here is the answer, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}


def cmd_counts(_args) -> int:
    report = {"source": _counts(source_pg_url()),
              "destination": _counts(store.get_pg_url())}
    src, dst = report["source"], report["destination"]
    if "error" not in src and "error" not in dst:
        report["delta"] = {
            "chunk_count": dst["chunk_count"] - src["chunk_count"],
            "run_count": dst["run_count"] - src["run_count"],
        }
    print(json.dumps(report, indent=2))
    return 0 if "error" not in report["destination"] else 1


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

def cmd_copy(args) -> int:
    import psycopg

    store.ensure_exec_schema()

    select_cols = ", ".join(
        "embedding::text AS embedding" if c == "embedding" else c
        for c in (*COLUMNS, "embedding"))
    export_sql = f"COPY (SELECT {select_cols} FROM executions) TO STDOUT"

    print(f"source      : {source_pg_url().rsplit('@', 1)[-1]}", file=sys.stderr)
    print(f"destination : {store.get_pg_url().rsplit('@', 1)[-1]}", file=sys.stderr)

    # Read the whole export into memory before writing a single destination
    # row. ~250 MB on 24.7k rows (a 768-dim vector is ~7.5 KB as text), which
    # is cheap next to the alternative: streaming source->destination keeps a
    # transaction open on BOTH clusters for the duration, and a stall on
    # either leaves a half-filled staging table whose state nothing records.
    buf = io.BytesIO()
    with psycopg.connect(source_pg_url()) as src:
        with src.cursor().copy(export_sql) as copy:
            for chunk in copy:
                buf.write(bytes(chunk))
    payload = buf.getvalue()
    print(f"exported    : {len(payload)/1e6:.1f} MB", file=sys.stderr)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "exported_bytes": len(payload)}, indent=2))
        return 0

    with psycopg.connect(store.get_pg_url()) as dst:
        # A bigger maintenance_work_mem is for the HNSW build, which
        # ensure_exec_schema already did on an empty table — kept here
        # because a re-run on a non-empty destination may still need it for
        # the implicit index maintenance of a large insert.
        dst.execute("SET maintenance_work_mem = '512MB'")
        dst.execute(f"DROP TABLE IF EXISTS {STAGING}")
        dst.execute(f"""
            CREATE UNLOGGED TABLE {STAGING} (
                chunk_id   TEXT,
                run_id     TEXT,
                seq        INT,
                content    TEXT,
                metadata   JSONB,
                indexed_at TIMESTAMPTZ,
                embedding  TEXT
            )
        """)
        import_cols = ", ".join((*COLUMNS, "embedding"))
        with dst.cursor().copy(f"COPY {STAGING} ({import_cols}) FROM STDIN") as copy:
            copy.write(payload)
        staged = dst.execute(f"SELECT COUNT(*) FROM {STAGING}").fetchone()[0]

        # ON CONFLICT DO NOTHING, never DO UPDATE: dual-write is live by the
        # time this runs, so a destination row that already exists came from
        # a write NEWER than this export and must win.
        cur = dst.execute(f"""
            INSERT INTO executions ({import_cols})
            SELECT chunk_id, run_id, seq, content, metadata, indexed_at,
                   embedding::vector
            FROM   {STAGING}
            ON CONFLICT (chunk_id) DO NOTHING
        """)
        inserted = cur.rowcount
        dst.execute(f"DROP TABLE IF EXISTS {STAGING}")
        dst.commit()
        dst.execute("ANALYZE executions")

    print(json.dumps({"staged": staged, "inserted": inserted,
                      "skipped_existing": staged - inserted,
                      "exported_bytes": len(payload)}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------

def _source_search(query: str, n_results: int) -> list[dict]:
    """Search the OLD home through its own MCP-backed pgvector path.

    aw-app-kb exposes execution search only as an MCP tool, not as an HTTP
    route, so its scores cannot be read over HTTP. Going straight to its
    Postgres would use THIS process's embedder for both sides and prove
    nothing about the source's. Instead this runs the query through the
    kb container's own Python — its real ``kb_pg`` embedder — and reads the
    scores out of ``exec_pg.search``.
    """
    import subprocess
    code = (
        "import json, sys\n"
        "from kb_app import exec_pg\n"
        "q = json.loads(sys.stdin.read())\n"
        "rows = exec_pg.search(q['query'], n_results=q['n'])\n"
        "print(json.dumps([{'chunk_id': r['chunk_id'], 'score': r['score']} for r in rows]))\n"
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", os.environ.get("KB_CONTAINER", "aw-app-kb"),
         "python3", "-c", code],
        input=json.dumps({"query": query, "n": n_results}),
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"source search failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _dest_search(query: str, n_results: int) -> list[dict]:
    import httpx
    r = httpx.post(f"{exec_index_url()}/search",
                   json={"query": query, "n_results": n_results}, timeout=120.0)
    r.raise_for_status()
    return [{"chunk_id": x["chunk_id"], "score": x["score"]}
            for x in (r.json().get("results") or [])]


def cmd_parity(args) -> int:
    queries = list(DEFAULT_QUERIES)
    if args.queries_file:
        queries = [line.strip() for line in Path(args.queries_file).read_text(
            encoding="utf-8").splitlines() if line.strip()]

    report = {"queries": [], "n_results": args.n_results,
              "max_abs_score_delta": 0.0, "rank_mismatches": 0, "ok": True}
    for query in queries:
        entry: dict = {"query": query}
        try:
            src = _source_search(query, args.n_results)
            dst = _dest_search(query, args.n_results)
        except Exception as exc:  # noqa: BLE001 — a failed probe is a reported failure
            entry["error"] = f"{type(exc).__name__}: {exc}"
            report["ok"] = False
            report["queries"].append(entry)
            continue

        src_ids = [r["chunk_id"] for r in src]
        dst_ids = [r["chunk_id"] for r in dst]
        entry["source_hits"] = len(src)
        entry["destination_hits"] = len(dst)
        entry["same_ranking"] = src_ids == dst_ids

        # Score identity is checked per chunk id, not per rank: a chunk the
        # destination has and the source does not (anything indexed since the
        # export) legitimately shifts the ranking without meaning the
        # embedding path diverged. What must NOT differ is the score of a
        # chunk BOTH stores hold — same model, same vector, same arithmetic.
        src_scores = {r["chunk_id"]: r["score"] for r in src}
        deltas = {r["chunk_id"]: abs(r["score"] - src_scores[r["chunk_id"]])
                  for r in dst if r["chunk_id"] in src_scores}
        entry["compared_chunks"] = len(deltas)
        entry["max_abs_score_delta"] = max(deltas.values()) if deltas else None
        if deltas:
            report["max_abs_score_delta"] = max(
                report["max_abs_score_delta"], max(deltas.values()))
            worst = max(deltas, key=deltas.get)
            entry["worst_chunk"] = {"chunk_id": worst, "delta": deltas[worst],
                                    "source_score": src_scores[worst]}
        if not entry["same_ranking"]:
            report["rank_mismatches"] += 1
            entry["destination_only"] = [c for c in dst_ids if c not in src_scores]
            entry["source_only"] = [c for c in src_ids if c not in set(dst_ids)]
        report["queries"].append(entry)

    # The threshold is float32 representation noise, nothing more: pgvector
    # stores float4, and both sides read the same stored bytes, so the
    # arithmetic should agree to ~1e-7. Anything above this is a real
    # divergence in the embedding path and must be investigated, NOT absorbed
    # by raising the number (design §3.3 step 4, §6).
    report["score_identity_tolerance"] = args.tolerance
    if report["max_abs_score_delta"] > args.tolerance:
        report["ok"] = False
        report["verdict"] = (
            f"SCORE DRIFT: max |delta| {report['max_abs_score_delta']:.3g} exceeds "
            f"{args.tolerance:.3g}. Same model and same vectors must give identical "
            f"scores — this is an embedding-path bug (task prefix, truncation bound, "
            f"or fastembed revision), not noise. Do not raise the tolerance.")
    elif report["ok"]:
        report["verdict"] = (
            f"score-identical within {args.tolerance:.3g} across "
            f"{sum(q.get('compared_chunks') or 0 for q in report['queries'])} "
            f"chunks shared by both stores")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("counts", help="chunk/run counts on both sides")

    p_copy = sub.add_parser("copy", help="COPY the rows across (idempotent)")
    p_copy.add_argument("--batch", type=int, default=0,
                        help="unused; the export is one COPY stream")
    p_copy.add_argument("--dry-run", action="store_true",
                        help="export and report size, write nothing")

    p_par = sub.add_parser("parity", help="score-identity check")
    p_par.add_argument("--queries-file", help="one query per line")
    p_par.add_argument("--n-results", type=int, default=10)
    p_par.add_argument("--tolerance", type=float, default=1e-6)

    args = parser.parse_args(argv)
    return {"counts": cmd_counts, "copy": cmd_copy, "parity": cmd_parity}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
