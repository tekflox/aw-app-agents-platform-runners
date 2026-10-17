"""Embedding helpers for the execution index — a deliberate verbatim lift of
``aw-app-kb``'s ``kb_app/kb_pg.py`` (``_get_model``/``_embed_docs``/
``_embed_query``/``_vec_str``, plus ``VECTOR_DIM`` and ``_EMBED_MAX_CHARS``).

Design: ``docs/design/execution-index-migration-off-aw-app-kb.md`` §3.1 —
"keep the 1500-char write refusal, the task prefixes, and the full-replace
upsert semantics byte-for-byte; they are contracts, not incidentals".

Why copy instead of import: aw-app-kb is a Tier-2 container app being retired;
nothing in this workspace process can import from it, and the whole point of
the migration is that this index outlives it. Why copy instead of re-deriving:
``nomic-embed-text-v1.5`` is task-prefix aware — documents and queries live in
different sub-spaces — so a missing ``search_query:`` prefix silently degrades
recall with no error anywhere. Likewise ``_vec_str``'s ``.8g`` is what the
24.8k already-stored vectors were written with, so changing it would make a
freshly-embedded chunk incomparable with a migrated one at float precision.
Any divergence here is invisible in tests and visible only as worse search, so
the code is the contract. ``scripts/migrate_execution_index.py``'s
score-identity parity check (design §3.3 step 4) is what proves it stayed put.

This module is loaded ONCE, in the ``exec-index`` managed subprocess
(``exec_index_service.py``) — never in the workspace's own 10 uvicorn
workers. The model is ~520 MB resident per process that loads it; that
arithmetic is the entire reason the design chose a subprocess over embedding
in-process (design §4, "Embed in-process in the runners app" — rejected).
"""
from __future__ import annotations

VECTOR_DIM = 768          # nomic-embed-text-v1.5
_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"   # float32, 0.52 GB, 768 dims, 8192-token ctx

# nomic has 8192-token context but O(n²) attention memory. Capping input at
# 1 500 chars (≈400 tokens) bounds the per-document peak RSS to ~500 MB.
# This is also the bound ``exec_index_store.upsert_chunks`` REFUSES past
# rather than truncating — see its docstring.
EMBED_MAX_CHARS = 1500

_model = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        # threads=2: ONNX intra-op parallelism across the available cores.
        _model = TextEmbedding(_MODEL_NAME, threads=2)
    return _model


def warm() -> None:
    """Load the model now rather than on the first request.

    Called from the service's lifespan: the first ``_get_model()`` pays a
    cold ONNX init (measured at ~18s for a single trivial chunk through
    aw-app-kb's POST route, which is the whole reason the producer's
    per-call POST timeout is 120s — see ``execution_index.KB_POST_TIMEOUT_S``).
    Paying it at boot instead means the first real run to finish does not.
    """
    _get_model()


def embed_docs(texts: list[str]) -> list[list[float]]:
    """Embed texts for **indexing** (``search_document:`` prefix)."""
    model = _get_model()
    prefixed = ["search_document: " + t[:EMBED_MAX_CHARS] for t in texts]
    # batch_size=1 prevents ONNX from padding all seqs to the longest in the
    # batch, which would multiply the memory overhead by the batch size.
    return [list(float(x) for x in v) for v in model.embed(prefixed, batch_size=1)]


def embed_query(text: str) -> list[float]:
    """Embed a **search query** (``search_query:`` prefix)."""
    model = _get_model()
    vecs = list(model.embed(["search_query: " + text]))
    return [float(x) for x in vecs[0]]


def vec_str(vec: list[float]) -> str:
    """Serialise a float list to a pgvector literal: ``[1.0,2.0,...]``."""
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"
