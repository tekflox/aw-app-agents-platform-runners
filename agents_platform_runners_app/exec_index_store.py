"""PostgreSQL store for the execution index — the new home of the
``executions`` table after it leaves ``aw-app-kb``'s bundled Postgres.

Design: ``docs/design/execution-index-migration-off-aw-app-kb.md``. A lift of
``aw-app-kb``'s ``kb_app/exec_pg.py`` with two differences, both forced by the
destination rather than chosen:

* **Where it connects.** aw-app-kb ran Postgres *inside its own container*
  (``entrypoint.sh``'s ``docker-entrypoint.sh postgres &``), so its URL was a
  hardcoded ``127.0.0.1:5432/knowledge_base``. The destination is the shared
  ``aw-remote-host-postgres`` — PostgreSQL **16.14**, pgvector, already hosting
  ``aw_workspace``/``awserv``/``ux_proto``. See :func:`get_pg_url` for why the
  URL is *derived* from the workspace's own ``AW_WORKSPACE_DB_URL`` rather
  than configured: it means this migration introduces no new credential
  anywhere — not in config, not in a file, not in the code. The design's
  §3.1 sketch said ``127.0.0.1:5432``; measured 2026-10-17, loopback:5432 is
  **refused** inside the workspace container (that publication is on the
  podman host, which is a *different* container) — the reachable address is
  ``aw-remote-host-postgres:5432``, which is exactly what the env var already
  holds.
* **It creates its own database.** ``CREATE DATABASE execution_index`` cannot
  run from inside the database it creates, so :func:`ensure_exec_schema`
  connects to the server's ``postgres`` db first. The design flagged "a
  recreated Postgres with a lost volume is now an index outage" (§5) — doing
  the create here rather than by hand makes that self-healing instead of a
  runbook step nobody will find.

Everything else is deliberately byte-for-byte: the ``chunk_id`` PK, the
full-replace upsert, the HNSW/btree indexes, the WHERE-clause filters, the
retention sweep, and the 1500-char **refusal** (never a truncation — see
:func:`upsert_chunks`).

**HNSW is rebuilt here, never moved.** Postgres does not transport index data
between clusters; only rows move (design §3.2 step 1).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from typing import Any

from . import exec_index_embed as _embed

log = logging.getLogger("aw_apps.agents_platform_runners.exec_index_store")

VECTOR_DIM = _embed.VECTOR_DIM
EMBED_MAX_CHARS = _embed.EMBED_MAX_CHARS

#: The database this index lives in on the shared server. A db per concern,
#: following ``aw_workspace``/``ux_proto``/``awserv`` already there.
DB_NAME = "execution_index"

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_MAX_ROWS = 80_000

_conn = None


# ------------------------------------------------------------------
# Connection URL
# ------------------------------------------------------------------

def _normalise(url: str) -> str:
    """Strip SQLAlchemy's ``+psycopg`` driver suffix — psycopg itself rejects
    it. ``src/api/db.py`` normalises in the other direction for the same two
    env vars, so both forms turn up in the wild."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _with_database(url: str, database: str) -> str:
    parts = urllib.parse.urlsplit(_normalise(url))
    return urllib.parse.urlunsplit(parts._replace(path="/" + database))


def get_pg_url() -> str:
    """Connection URL for the ``execution_index`` database.

    Resolution order, first hit wins:

    1. ``EXEC_INDEX_PG_URL`` — an explicit override, for a deployment whose
       index does not live next to its workspace journal.
    2. ``AW_WORKSPACE_DB_URL`` / ``AWSERV_DB_URL`` with the database name
       swapped to :data:`DB_NAME`. These are set on the workspace container
       by the aw-remote-host runtime and already point at the shared
       ``aw-remote-host-postgres`` **with its password**.

    (2) is the load-bearing one and the reason this module takes no secret of
    its own: the credential this service needs is the credential the process
    that supervises it already has. Writing it into app config or an env file
    would create a second copy to rotate, which is exactly the thing the
    workspace's secret-store rules exist to prevent.
    """
    explicit = os.environ.get("EXEC_INDEX_PG_URL", "").strip()
    if explicit:
        return _normalise(explicit)
    base = (os.environ.get("AW_WORKSPACE_DB_URL")
            or os.environ.get("AWSERV_DB_URL") or "").strip()
    if not base:
        raise RuntimeError(
            "neither EXEC_INDEX_PG_URL nor AW_WORKSPACE_DB_URL/AWSERV_DB_URL is set — "
            "the execution index has no Postgres to connect to. On a workspace host "
            "these are set by the aw-remote-host runtime; this service is meant to run "
            "as a managed subprocess of the workspace process, which inherits them.")
    return _with_database(base, DB_NAME)


def _admin_url() -> str:
    """Server-level URL (the ``postgres`` db) used only to CREATE DATABASE."""
    return _with_database(get_pg_url(), "postgres")


# ------------------------------------------------------------------
# Connection singleton (lazy, reconnects on error)
# ------------------------------------------------------------------

def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg
        _conn = psycopg.connect(get_pg_url())
    return _conn


def _reset_conn() -> None:
    global _conn
    try:
        if _conn and not _conn.closed:
            _conn.close()
    except Exception:
        pass
    _conn = None


# ------------------------------------------------------------------
# Schema bootstrap
# ------------------------------------------------------------------

def ensure_database() -> bool:
    """Create the :data:`DB_NAME` database if it is missing. True if created.

    Raises whatever psycopg raises — the caller
    (:func:`ensure_exec_schema`) owns the retry/log policy.
    """
    import psycopg
    with psycopg.connect(_admin_url(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)).fetchone()
        if row:
            return False
        # No parameter binding for an identifier; DB_NAME is this module's
        # own constant, never caller input.
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    return True


def ensure_exec_schema(retries: int = 12, delay: float = 1.0) -> None:
    """Create the database, extension, table and indexes if needed.

    Retries so the service can start before Postgres is reachable (a shared
    container that is itself restarting). Failures are logged but never
    raised — same contract as ``exec_pg.ensure_exec_schema``: the service
    still comes up and its ``/status`` is what says the store is unusable,
    rather than a dead process with the reason in a log nobody reads.
    """
    for attempt in range(retries):
        try:
            if ensure_database():
                log.info("exec_index: created database %s", DB_NAME)
            import psycopg
            with psycopg.connect(get_pg_url(), autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS executions (
                        chunk_id   TEXT PRIMARY KEY,
                        run_id     TEXT NOT NULL,
                        seq        INT  NOT NULL,
                        content    TEXT NOT NULL,
                        metadata   JSONB NOT NULL DEFAULT '{{}}',
                        indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        embedding  vector({VECTOR_DIM})
                    )
                """)
                # HNSW: fast approximate NN, works on empty tables. Built
                # here, at the destination — index data never moves between
                # clusters (design §3.2).
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_embedding_hnsw
                    ON executions USING hnsw (embedding vector_cosine_ops)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_run_id
                    ON executions (run_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS executions_indexed_at
                    ON executions (indexed_at)
                """)
            log.info("exec_index: schema ready (db=%s)", DB_NAME)
            return
        except Exception as exc:
            log.warning(
                "exec_index: schema init attempt %d/%d failed: %s — retrying in %.1fs",
                attempt + 1, retries, exc, delay,
            )
            time.sleep(delay)
    log.error("exec_index: schema init failed — the shared Postgres may be unreachable")


# ------------------------------------------------------------------
# Public API — same signatures as aw-app-kb's exec_pg
# ------------------------------------------------------------------

def count() -> int:
    """Total number of indexed execution chunks (0 when unreadable)."""
    try:
        row = _get_conn().execute("SELECT COUNT(*) FROM executions").fetchone()
        return row[0] if row else 0
    except Exception:
        _reset_conn()
        return 0


def upsert_chunks(run_id: str, chunks: list[dict[str, Any]]) -> int:
    """Embed and upsert one run's chunks.

    Idempotent AND a full replace: re-indexing the same ``run_id`` overwrites
    its previous chunks rather than appending duplicates, and also deletes any
    old chunk whose ``seq`` is absent from this call. The ``chunk_id`` upsert
    is what makes the cutover's dual-write safe and re-orderable (design §3.3).

    Raises
    ------
    ValueError
        If ``run_id`` is empty, a chunk is missing ``seq``/``content``, or any
        chunk's content exceeds :data:`EMBED_MAX_CHARS`. **A refusal, not a
        truncation** — an auto-truncating upsert reports success while being
        semantically invisible past 1500 chars, which is the exact bug this
        check exists to stop (design §6).
    Exception
        Whatever the driver raises, re-raised after ``_reset_conn()`` — a
        failed *write* must not report success, and the module-global
        connection must not be left poisoned for the next caller.
    """
    if not run_id:
        raise ValueError("run_id is required")
    if not chunks:
        return 0

    for c in chunks:
        if c.get("seq") is None or not c.get("content"):
            raise ValueError(f"chunk missing seq/content: {c!r}")

    oversized = [c["seq"] for c in chunks if len(c["content"]) > EMBED_MAX_CHARS]
    if oversized:
        raise ValueError(
            f"chunk(s) {oversized} for run {run_id!r} exceed {EMBED_MAX_CHARS} chars "
            f"(the embedder reads only the first {EMBED_MAX_CHARS} chars and would "
            f"report success regardless) — split them smaller before sending"
        )

    texts = [c["content"] for c in chunks]
    vectors = _embed.embed_docs(texts)

    conn = _get_conn()
    try:
        for chunk, vec in zip(chunks, vectors):
            chunk_id = f"{run_id}:{chunk['seq']}"
            vs = _embed.vec_str(vec)
            metadata = dict(chunk.get("metadata") or {})
            conn.execute(
                """
                INSERT INTO executions (chunk_id, run_id, seq, content, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (chunk_id) DO UPDATE
                    SET content    = EXCLUDED.content,
                        metadata   = EXCLUDED.metadata,
                        embedding  = EXCLUDED.embedding,
                        indexed_at = now()
                """,
                (chunk_id, run_id, chunk["seq"], chunk["content"], json.dumps(metadata), vs),
            )

        seqs = [c["seq"] for c in chunks]
        conn.execute(
            "DELETE FROM executions WHERE run_id = %s AND seq != ALL(%s)",
            (run_id, seqs),
        )
        conn.commit()
    except Exception as exc:
        log.error("exec_index: upsert_chunks error for run %s: %s", run_id, exc)
        _reset_conn()
        raise
    return len(chunks)


def delete_run(run_id: str) -> int:
    """Delete every chunk for one run. Returns the number of rows removed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM executions WHERE run_id = %s", (run_id,))
    conn.commit()
    return cur.rowcount


def search(query: str, n_results: int = 5,
           filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Cosine-similarity search over indexed execution chunks.

    ``filters`` (all optional, AND-combined, applied in the SQL WHERE — never
    as a post-filter over an overfetch): ``run_id``, ``status``,
    ``agent_slug``, ``target_slug`` (matched against ``metadata->>'...'``),
    ``since_days`` (matched against ``indexed_at``).

    Returns ``{chunk_id, run_id, seq, content, metadata, score}`` dicts.
    """
    filters = filters or {}
    where: list[str] = []
    params: list[Any] = []

    if filters.get("run_id"):
        where.append("run_id = %s")
        params.append(filters["run_id"])
    if filters.get("status"):
        where.append("metadata->>'status' = %s")
        params.append(filters["status"])
    if filters.get("agent_slug"):
        where.append("metadata->>'agent_slug' = %s")
        params.append(filters["agent_slug"])
    if filters.get("target_slug"):
        where.append("metadata->>'target_slug' = %s")
        params.append(filters["target_slug"])
    since_days = filters.get("since_days")
    if since_days is not None:
        where.append("indexed_at >= now() - (%s || ' days')::interval")
        params.append(str(int(since_days)))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        vs = _embed.vec_str(_embed.embed_query(query))
        rows = _get_conn().execute(
            f"""
            SELECT chunk_id, run_id, seq, content, metadata,
                   1 - (embedding <=> %s::vector) AS score
            FROM   executions
            {where_sql}
            ORDER  BY embedding <=> %s::vector
            LIMIT  %s
            """,
            (vs, *params, vs, n_results),
        ).fetchall()
        return [
            {
                "chunk_id": r[0],
                "run_id": r[1],
                "seq": r[2],
                "content": r[3],
                "metadata": r[4] or {},
                "score": float(r[5]),
            }
            for r in rows
        ]
    except Exception as exc:
        log.error("exec_index: search error: %s", exc)
        _reset_conn()
        return []


def prune(retention_days: int | None = None,
          max_rows: int | None = None) -> dict[str, int]:
    """Delete chunks older than ``retention_days``, then — if still over
    ``max_rows`` — the oldest remaining down to that cap. ``None`` for either
    reads the env var (or this module's default) at call time, so a config
    change takes effect on the next sweep with no redeploy.

    Returns ``{"deleted_by_age": int, "deleted_by_cap": int}``.
    """
    if retention_days is None:
        retention_days = int(os.environ.get(
            "EXEC_INDEX_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
    if max_rows is None:
        max_rows = int(os.environ.get("EXEC_INDEX_MAX_ROWS", _DEFAULT_MAX_ROWS))

    deleted_by_age = 0
    deleted_by_cap = 0
    try:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM executions WHERE indexed_at < now() - (%s || ' days')::interval",
            (str(int(retention_days)),),
        )
        deleted_by_age = cur.rowcount
        conn.commit()

        if max_rows is not None:
            row = conn.execute("SELECT COUNT(*) FROM executions").fetchone()
            total = row[0] if row else 0
            if total > max_rows:
                excess = total - max_rows
                cur = conn.execute(
                    """
                    DELETE FROM executions WHERE chunk_id IN (
                        SELECT chunk_id FROM executions ORDER BY indexed_at ASC LIMIT %s
                    )
                    """,
                    (excess,),
                )
                deleted_by_cap = cur.rowcount
                conn.commit()
    except Exception as exc:
        log.error("exec_index: prune error: %s", exc)
        _reset_conn()

    return {"deleted_by_age": deleted_by_age, "deleted_by_cap": deleted_by_cap}


def status() -> dict[str, Any]:
    """Observability snapshot: chunk/run counts and the indexed_at range.

    Same shape aw-app-kb's ``/api/kb/executions/status`` answers, so the
    cutover's count-parity check (design §3.3 step 4) compares like with like.
    ``error`` is added on failure rather than silently reporting zeros — a
    zero that means "unreachable" and a zero that means "empty" lead to
    opposite conclusions during a migration.
    """
    try:
        row = _get_conn().execute(
            "SELECT COUNT(*), COUNT(DISTINCT run_id), MIN(indexed_at), MAX(indexed_at) "
            "FROM executions"
        ).fetchone()
        if not row:
            row = (0, 0, None, None)
        return {
            "chunk_count": row[0] or 0,
            "run_count": row[1] or 0,
            "oldest_indexed_at": row[2].isoformat() if row[2] else None,
            "newest_indexed_at": row[3].isoformat() if row[3] else None,
        }
    except Exception as exc:
        log.error("exec_index: status error: %s", exc)
        _reset_conn()
        return {"chunk_count": 0, "run_count": 0, "oldest_indexed_at": None,
                "newest_indexed_at": None, "error": str(exc)}
