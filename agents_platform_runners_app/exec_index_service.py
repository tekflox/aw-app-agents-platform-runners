"""The ``exec-index`` managed service — the new home of the execution index's
embedder, write routes and search.

Design: ``docs/design/execution-index-migration-off-aw-app-kb.md`` §3.1.
Registered by ``plugin.py`` through ``ctx.services`` (``service:manage``), the
same supervisor aw-app-codegraphcontext / aw-app-proxy /
aw-app-google-workspace-mcp already use: one process, respawned on crash,
owned by exactly one worker's lease, stopped on uninstall.

**Why a subprocess and not in-process.** The workspace process runs
``AW_WORKSPACE_WORKERS`` uvicorn workers (10 here) and the fastembed model is
~520 MB *per process that loads it*. In-process would be up to ~5 GB resident
for a write-rarely workload. One subprocess holds it once.

**Why loopback only.** Both sides of this service live in the workspace
container: the producer (``execution_index.index_run``, in core) and the
Postgres client. Nothing outside needs to reach it, so it binds
``127.0.0.1`` and is unreachable from any other container by construction —
which is also why the shared-secret header is required on *writes* only. The
MCP tool, which runs as a stdio child of the **gateway's** container and so
has no route to this loopback at all, goes the long way round: through the
workspace API to this app's own ``/execution-index/search`` route, which
proxies here (see ``routes.py``).

Run it with::

    python3 -m agents_platform_runners_app.exec_index_service

``--host``/``--port`` come from ``EXEC_INDEX_HOST``/``EXEC_INDEX_PORT``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import exec_index_embed as embed_mod
from . import exec_index_store as store

log = logging.getLogger("aw_apps.agents_platform_runners.exec_index_service")

DEFAULT_PORT = 9408
DEFAULT_HOST = "127.0.0.1"

#: Retention sweep cadence. Owned by this service's own lifespan rather than
#: an external scheduled task, for the reason aw-app-kb's ``exec_pg`` gives:
#: retention must not be able to stop working in silence just because someone
#: deleted a task in a UI.
PRUNE_INTERVAL_S = 24 * 3600.0

#: Env var carrying the shared secret writes must present. Set by the
#: supervisor's inherited environment is NOT enough — ``plugin.py`` passes it
#: on the service's own command line via this var (see ``service_command``),
#: sourced from the app's ``execution_index_secret`` config key, which is
#: already an ``x-secret`` field held by the workspace secret store.
SECRET_ENV = "EXEC_INDEX_SECRET"


def port() -> int:
    try:
        return int(os.environ.get("EXEC_INDEX_PORT") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def host() -> str:
    return os.environ.get("EXEC_INDEX_HOST") or DEFAULT_HOST


def service_command(package_dir: str, config: dict | None = None) -> str:
    """The command line ``ctx.services.register`` supervises.

    ``sys.executable`` rather than a bare ``python3``: the deps this service
    imports (``fastembed``, ``psycopg``) are installed into the *workspace
    process's own* interpreter by ``runtime.pip_requires``, and a different
    ``python3`` on PATH would not have them. ``cwd`` is the app's package dir,
    set by the supervisor, which is what makes ``-m`` resolve.
    """
    del package_dir, config  # resolved by the supervisor / env, kept for symmetry
    return f"{sys.executable} -m agents_platform_runners_app.exec_index_service"


def _check_secret(presented: str | None) -> None:
    """Shared-secret gate on writes.

    An unconfigured secret means CLOSED (503), never open — the same posture
    as aw-app-kb's ``_check_exec_secret`` and this app's own
    ``require_execute_secret``. The service is loopback-only, so this is
    defence in depth rather than the only wall, but a write path that silently
    opens when its credential goes missing is how a config wipe becomes an
    unauthenticated endpoint.
    """
    configured = (os.environ.get(SECRET_ENV) or "").strip()
    if not configured:
        raise HTTPException(
            503, f"{SECRET_ENV} is not configured for the exec-index service — "
                 "set execution_index_secret in this app's Settings")
    if not presented or presented != configured:
        raise HTTPException(401, "unauthorized")


def build_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Schema first: a store that cannot be reached is the one failure
        # worth knowing about before the model spends 20s loading.
        await asyncio.to_thread(store.ensure_exec_schema)
        await asyncio.to_thread(embed_mod.warm)
        log.info("exec-index: ready on %s:%s (db=%s)", host(), port(), store.DB_NAME)
        task = asyncio.create_task(_prune_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _prune_loop() -> None:
        while True:
            try:
                await asyncio.sleep(PRUNE_INTERVAL_S)
                result = await asyncio.to_thread(store.prune)
                log.info("exec-index: retention sweep %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a sweep failure must not kill the service
                log.warning("exec-index: retention sweep failed", exc_info=True)

    app = FastAPI(title="exec-index", lifespan=lifespan)

    @app.get("/status")
    async def status() -> dict:
        """Liveness + store snapshot. No secret — this is what the doctor
        check reads to tell "the producer is configured to write here but
        nothing is listening" apart from "all good" (design §6)."""
        snapshot = await asyncio.to_thread(store.status)
        return {
            "ok": "error" not in snapshot,
            "database": store.DB_NAME,
            "vector_dim": store.VECTOR_DIM,
            "embed_max_chars": store.EMBED_MAX_CHARS,
            **snapshot,
        }

    @app.post("/executions")
    async def post_executions(
        data: dict = Body(...),
        x_exec_index_secret: str | None = Header(default=None),
    ):
        """Upsert one run's chunks. Body/response shape is identical to
        aw-app-kb's ``POST /api/kb/executions`` so the producer's dual-write
        sends one payload to both targets with no branching."""
        _check_secret(x_exec_index_secret)

        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            return JSONResponse({"error": "run_id is required"}, status_code=400)

        chunks_in = data.get("chunks")
        if not isinstance(chunks_in, list) or not chunks_in:
            return JSONResponse({"error": "chunks must be a non-empty list"}, status_code=400)

        metadata_common = data.get("metadata_common") or {}
        chunks = []
        for c in chunks_in:
            if not isinstance(c, dict) or c.get("seq") is None or not c.get("content"):
                return JSONResponse(
                    {"error": f"each chunk needs seq and content: {c!r}"}, status_code=400)
            meta = dict(metadata_common)
            meta.update(c.get("metadata") or {})
            chunks.append({"seq": c["seq"], "content": c["content"], "metadata": meta})

        try:
            n = await asyncio.to_thread(store.upsert_chunks, run_id, chunks)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 — a write must never report success
            log.warning("exec-index: failed to index run %s: %s", run_id, exc)
            return JSONResponse({"error": str(exc)}, status_code=500)
        return {"success": True, "run_id": run_id, "chunks_indexed": n}

    @app.post("/executions/prune")
    async def post_executions_prune(
        data: dict = Body(default={}),
        x_exec_index_secret: str | None = Header(default=None),
    ) -> dict:
        """Manual retention sweep — the same logic ``_prune_loop`` runs."""
        _check_secret(x_exec_index_secret)
        result = await asyncio.to_thread(
            store.prune, data.get("retention_days"), data.get("max_rows"))
        return {"success": True, **result}

    @app.get("/executions/status")
    async def get_executions_status() -> dict:
        """Read-only counts — mirrors aw-app-kb's own route of the same name,
        so the cutover parity check can diff the two directly."""
        return await asyncio.to_thread(store.status)

    @app.post("/search")
    async def post_search(request: Request, data: dict = Body(...)) -> dict:
        """Chunk-level semantic search. Grouping to one block per run is the
        MCP handler's job (``mcp_server.py``), exactly as it was aw-app-kb's
        — this returns raw chunk hits with scores so the parity check can
        compare scores rather than rendered text."""
        del request
        query = (data.get("query") or "").strip()
        if not query:
            return {"total": await asyncio.to_thread(store.count), "results": []}
        n_results = int(data.get("n_results") or 5)
        filters = {k: v for k, v in (data.get("filters") or {}).items() if v is not None}
        total = await asyncio.to_thread(store.count)
        results = await asyncio.to_thread(store.search, query, n_results, filters)
        return {"total": total, "results": results}

    return app


def main(argv: list[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=os.environ.get("EXEC_INDEX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    import uvicorn
    # Built here, not at module level: plugin.py imports this module just for
    # service_command(), inside the workspace process — instantiating a
    # FastAPI app (and a lifespan that would never run) in all ten workers
    # as an import side effect is pure waste.
    #
    # One worker, deliberately: the whole point of this process is that the
    # embedding model is resident exactly once.
    uvicorn.run(build_app(), host=host(), port=port(),
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
