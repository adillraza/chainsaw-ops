"""Knowledge Base search service.

Embeds the user's query via Vertex AI ``text-embedding-004`` (with the
``RETRIEVAL_QUERY`` task type — the asymmetric counterpart to the
``RETRIEVAL_DOCUMENT`` embeddings produced by
``scripts/kb_neto_products_ingest.py``), then runs BigQuery's native
``VECTOR_SEARCH`` against ``kb.documents``.

Returns a small dict per hit with everything the search-result UI needs
to render — title, snippet, brand/categories, public URL, similarity
score — so the route layer can pass the response straight through to
the template / JSON response.
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.cloud import bigquery

PROJECT  = "chainsawspares-385722"
DATASET  = "kb"
LOCATION = "us-central1"
EMBED_MODEL = "text-embedding-004"
SNIPPET_CHARS = 240

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vertex AI client — initialised lazily and cached. Loading the model is
# ~200ms; we don't want to pay it on every search.
# ---------------------------------------------------------------------------

_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        vertexai.init(project=PROJECT, location=LOCATION)
        _embed_model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
        log.info("kb: loaded embedding model %s", EMBED_MODEL)
    return _embed_model


def _embed_query(text: str) -> list[float]:
    """Embed ``text`` for retrieval querying. Cached per query string."""
    return _embed_query_cached(text.strip().lower())


@lru_cache(maxsize=512)
def _embed_query_cached(text: str) -> tuple[float, ...]:
    """LRU-cached embedding, keyed on lowercased+stripped query.

    A tuple is returned so it's hashable and immutable in the cache. Most
    agents repeat the same handful of queries during a shift, so caching
    here saves both latency (~150ms) and a Vertex API call.
    """
    from vertexai.language_models import TextEmbeddingInput
    inp = TextEmbeddingInput(text, task_type="RETRIEVAL_QUERY")
    result = _get_embed_model().get_embeddings([inp])[0]
    return tuple(result.values)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Return the top-K KB hits for ``query``, ranked by cosine similarity.

    Each hit is a flat dict:
      * ``doc_id`` / ``source`` / ``source_id`` / ``sku``
      * ``title`` — display label
      * ``url`` — link agents click through
      * ``snippet`` — first ~240 chars of body for context
      * ``metadata`` — brand, categories, etc. as a parsed dict
      * ``score`` — 0..1, higher is better (1 - cosine_distance)
    """
    query = (query or "").strip()
    if not query:
        return []
    t_total = time.perf_counter()

    t = time.perf_counter()
    vec = list(_embed_query(query))
    embed_ms = (time.perf_counter() - t) * 1000

    # Inline the query embedding as ``UNNEST(@q_embedding)`` rather than a
    # separate temp table — keeps the round-trip to one BQ job.
    sql = f"""
    WITH q AS (
      SELECT @q AS embedding
    )
    SELECT
      base.doc_id, base.source, base.source_id, base.sku,
      base.title, base.url, base.body, base.metadata,
      distance
    FROM VECTOR_SEARCH(
      TABLE `{PROJECT}.{DATASET}.documents`,
      'embedding',
      TABLE q,
      top_k => {int(top_k)},
      distance_type => 'COSINE'
    )
    ORDER BY distance ASC
    """

    bq = _bq_client()
    t = time.perf_counter()
    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("q", "FLOAT64", vec),
        ]),
    )
    rows = list(job.result())
    bq_ms = (time.perf_counter() - t) * 1000

    hits: list[dict[str, Any]] = []
    for r in rows:
        body = r.body or ""
        snippet = body[:SNIPPET_CHARS]
        if len(body) > SNIPPET_CHARS:
            snippet = snippet.rsplit(" ", 1)[0] + "…"

        metadata = r.metadata
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        elif metadata is None:
            metadata = {}

        # cosine distance ∈ [0, 2]; convert to a similarity score ∈ [0, 1]
        # so the UI can show a more intuitive "relevance" bar.
        score = max(0.0, 1.0 - float(r.distance) / 2.0)

        hits.append({
            "doc_id":    r.doc_id,
            "source":    r.source,
            "source_id": r.source_id,
            "sku":       r.sku,
            "title":     r.title,
            "url":       r.url,
            "snippet":   snippet,
            "metadata":  metadata,
            "score":     round(score, 4),
        })

    total_ms = (time.perf_counter() - t_total) * 1000
    log.info("kb.search q=%r top_k=%d hits=%d  embed=%.0fms bq=%.0fms total=%.0fms",
             query, top_k, len(hits), embed_ms, bq_ms, total_ms)
    return hits


# ---------------------------------------------------------------------------
# BQ client — reuse the singleton owned by purchase_orders_service so we
# don't open a second connection pool per search.
# ---------------------------------------------------------------------------

def _bq_client() -> bigquery.Client:
    from app.services.purchase_orders_service import purchase_orders_service
    return purchase_orders_service.client
