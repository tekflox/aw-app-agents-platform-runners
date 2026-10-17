"""The exec-index service and its store, at the seams a live run cannot show.

docs/design/execution-index-migration-off-aw-app-kb.md §3.1/§3.2. No Postgres
and no embedding model here — those are exercised live (the design's §6 is
explicit that CI cannot see any of the seams this crosses). What IS testable
offline is everything that decides *where* it connects and *what it refuses*,
and both have already cost someone time:

* the design sketched ``127.0.0.1:5432`` for the destination, which is
  **refused** inside the workspace container (that publication belongs to the
  podman host, a different container) — the reachable address is the one the
  workspace's own ``AW_WORKSPACE_DB_URL`` already carries;
* the 1500-char bound is a **refusal**, and auto-truncating it "to be helpful"
  recreates the bug it exists to stop.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents_platform_runners_app import exec_index_embed as embed  # noqa: E402
from agents_platform_runners_app import exec_index_service as svc  # noqa: E402
from agents_platform_runners_app import exec_index_store as store  # noqa: E402

_WS_URL = "postgresql+psycopg://postgres:pw@aw-remote-host-postgres:5432/aw_workspace"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("EXEC_INDEX_PG_URL", "AW_WORKSPACE_DB_URL", "AWSERV_DB_URL",
                "EXEC_INDEX_PORT", "EXEC_INDEX_HOST", svc.SECRET_ENV):
        monkeypatch.delenv(key, raising=False)


def test_the_url_is_derived_from_the_workspaces_own_db_url(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_DB_URL", _WS_URL)
    url = store.get_pg_url()
    # Same server, same credential, different database. That is the whole
    # reason this migration introduces no new secret anywhere: the credential
    # it needs is the one the process supervising it already holds.
    assert url == "postgresql://postgres:pw@aw-remote-host-postgres:5432/execution_index"
    # psycopg rejects SQLAlchemy's +psycopg suffix; src/api/db.py normalises
    # the same two env vars in the OTHER direction, so both forms are live.
    assert "+psycopg" not in url


def test_awserv_db_url_is_the_fallback_and_an_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("AWSERV_DB_URL", _WS_URL)
    assert store.get_pg_url().endswith("/execution_index")

    monkeypatch.setenv("EXEC_INDEX_PG_URL", "postgresql://u:p@elsewhere:5555/ei")
    assert store.get_pg_url() == "postgresql://u:p@elsewhere:5555/ei"


def test_no_db_url_at_all_raises_rather_than_guessing_loopback():
    # Guessing 127.0.0.1:5432 is exactly the wrong fallback here: inside the
    # workspace container nothing listens there, so the service would spend
    # its retry budget on an address that can never work and report a
    # Postgres outage instead of a missing configuration.
    with pytest.raises(RuntimeError) as exc:
        store.get_pg_url()
    assert "AW_WORKSPACE_DB_URL" in str(exc.value)


def test_the_admin_url_targets_the_server_not_the_index_database(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_DB_URL", _WS_URL)
    # CREATE DATABASE cannot run from inside the database it creates.
    assert store._admin_url().endswith("/postgres")


def test_an_unconfigured_secret_closes_the_write_path_rather_than_opening_it():
    # A write path that silently opens when its credential goes missing is
    # how a config wipe becomes an unauthenticated endpoint.
    with pytest.raises(HTTPException) as exc:
        svc._check_secret("anything")
    assert exc.value.status_code == 503


def test_a_wrong_or_missing_secret_is_401(monkeypatch):
    monkeypatch.setenv(svc.SECRET_ENV, "right")
    with pytest.raises(HTTPException) as exc:
        svc._check_secret("wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        svc._check_secret(None)
    svc._check_secret("right")  # no raise


def test_the_service_runs_on_the_interpreter_that_has_its_deps():
    cmd = svc.service_command("/opt/aw-workspace/apps/agents-platform-runners", {})
    # fastembed/psycopg are installed into the WORKSPACE process's own
    # interpreter by runtime.pip_requires. A bare "python3" on PATH is not
    # guaranteed to be that interpreter, and the failure mode is an
    # ImportError in a subprocess log nobody is watching.
    assert cmd.startswith(sys.executable + " ")
    assert cmd.endswith("-m agents_platform_runners_app.exec_index_service")


def test_port_and_host_default_to_loopback_9408(monkeypatch):
    assert (svc.host(), svc.port()) == ("127.0.0.1", 9408)
    monkeypatch.setenv("EXEC_INDEX_PORT", "not-a-number")
    assert svc.port() == 9408  # a typo must not take the service down
    monkeypatch.setenv("EXEC_INDEX_PORT", "9999")
    assert svc.port() == 9999


def test_the_embedding_contract_matches_the_old_home_exactly():
    # These four values ARE the compatibility contract with the 24.8k vectors
    # being migrated: dimension, the truncation bound, and the two task
    # prefixes. nomic-embed-text-v1.5 puts documents and queries in different
    # sub-spaces, so a missing prefix degrades recall with no error anywhere
    # — which is why the live parity check compares scores, not just counts.
    assert embed.VECTOR_DIM == 768
    assert embed.EMBED_MAX_CHARS == 1500
    assert embed._MODEL_NAME == "nomic-ai/nomic-embed-text-v1.5"

    src = (Path(ROOT) / "agents_platform_runners_app" / "exec_index_embed.py").read_text()
    assert '"search_document: " + t[:EMBED_MAX_CHARS]' in src
    assert '"search_query: " + text' in src
    assert "batch_size=1" in src


def test_vec_str_keeps_the_precision_the_stored_vectors_were_written_with():
    # .8g is what aw-app-kb wrote all 24.8k vectors with. Changing it would
    # make a freshly-embedded chunk incomparable with a migrated one at float
    # precision, and the parity check's tolerance is 1e-6.
    assert embed.vec_str([1.0, -0.5, 1 / 3]) == "[1,-0.5,0.33333333]"


def test_oversized_content_is_refused_not_truncated(monkeypatch):
    # Reaching the ValueError without a database: the length check runs
    # before any connection or embedding.
    calls = []
    monkeypatch.setattr(store._embed, "embed_docs", lambda t: calls.append(t) or [])
    with pytest.raises(ValueError) as exc:
        store.upsert_chunks("run-1", [
            {"seq": 0, "content": "x" * (store.EMBED_MAX_CHARS + 1)}])
    assert "exceed 1500 chars" in str(exc.value)
    assert calls == []  # refused before it cost an embedding


def test_an_empty_run_id_or_missing_field_is_refused():
    with pytest.raises(ValueError):
        store.upsert_chunks("", [{"seq": 0, "content": "x"}])
    with pytest.raises(ValueError):
        store.upsert_chunks("run-1", [{"seq": 0}])
    with pytest.raises(ValueError):
        store.upsert_chunks("run-1", [{"content": "x"}])
    assert store.upsert_chunks("run-1", []) == 0  # nothing to do is not an error
