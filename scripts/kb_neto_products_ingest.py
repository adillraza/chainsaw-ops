"""Phase 1A KB ingest — Neto products → Vertex AI embeddings → BQ kb.documents.

Run by hand for the first pass, then by ``kb-refresh.timer`` hourly.

Flow per run:

1. Look up the watermark in ``kb.refresh_state`` for ``source='neto_product'``.
2. Pull rows from ``dataform.neto_product_list`` where:
   * ``Approved='True' AND IsActive='True'`` (the same filter the live site uses)
   * AND ``DateUpdated > watermark`` if a watermark exists; full pull otherwise
3. For each row, build a structured text representation and push them through
   ``text-embedding-004`` in batches.
4. MERGE the rows into ``kb.documents`` keyed on ``doc_id`` so updates replace
   in place rather than appending.
5. Bump the watermark to NOW() on success.

The first run does a full reload (~11k products, ~$0.36 in embeddings, ~5–10
minutes). Subsequent hourly runs typically hit a handful of changed rows.

Usage:
    python3 scripts/kb_neto_products_ingest.py [--reset] [--limit N]

    --reset   Clears the watermark before running (forces a full reload).
    --limit N Process at most N products (handy for first-run sanity checks).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Iterable

from google.cloud import bigquery
import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

PROJECT  = "chainsawspares-385722"
DATASET  = "kb"
LOCATION = "us-central1"
SOURCE   = "neto_product"

# text-embedding-004 = 768 dims, supports task-type so retrieval quality is
# better when documents are embedded with RETRIEVAL_DOCUMENT and the live
# query uses RETRIEVAL_QUERY (kb_service.py).
EMBED_MODEL = "text-embedding-004"
# Vertex caps each request at 20,000 tokens total. We batch by total
# char count (assuming a conservative ~2.5 chars/token for product copy
# heavy in model numbers and codes), targeting 35k chars per request
# (≈14k tokens). Some product descriptions individually exceed the
# per-doc 2048-token cap; we truncate those to MAX_CHARS_PER_DOC.
MAX_CHARS_PER_DOC   = 4500
MAX_CHARS_PER_BATCH = 35_000
EMBED_BATCH_FALLBACK = 25      # used only if the bin-packer can't fit one doc
MERGE_BATCH = 500


# ---------------------------------------------------------------------------
# Source query
# ---------------------------------------------------------------------------

def fetch_products(bq: bigquery.Client, since: datetime | None,
                   limit: int | None) -> list[dict]:
    """Pull products needing (re)embedding from the Dataform table."""
    where = ["Approved = 'True'", "IsActive = 'True'"]
    params: list[bigquery.ScalarQueryParameter] = []
    if since is not None:
        # DateUpdated is a STRING in source — null + '0000-00-00…' both mean
        # "never set", so SAFE_CAST to TIMESTAMP, NULL on parse failure.
        where.append(
            "SAFE.PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', "
            "NULLIF(DateUpdated, '0000-00-00 00:00:00')) > @since"
        )
        params.append(bigquery.ScalarQueryParameter("since", "TIMESTAMP", since))
    sql = f"""
    SELECT
      ID, SKU, UPC AS Barcode, Name, Brand, Model, Subtitle,
      Description, ItemSpecifics, Categories, RelatedContents,
      ItemURL, ModelNumber, CubicWeight,
      ItemWidth, ItemLength, ItemHeight,
      ShippingLength, ShippingWidth, ShippingHeight, ShippingWeight,
      RequiresPackaging, is_kitted_item, kit_components, product_type,
      ShippingCategoryName, Images,
      DateUpdated
    FROM `{PROJECT}.dataform.neto_product_list`
    WHERE {' AND '.join(where)}
    """
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = [dict(r) for r in job.result()]
    return rows


# ---------------------------------------------------------------------------
# Chunk text construction — what we feed the embedding model
# ---------------------------------------------------------------------------

_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE   = re.compile(r"\s+")


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = _HTML_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return _WS_RE.sub(" ", s).strip()


def _parse_item_specifics(v) -> list[tuple[str, str]]:
    """ItemSpecifics is JSON — return [(name, value), ...]. Empty on None/garbage."""
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    out: list[tuple[str, str]] = []
    for entry in v if isinstance(v, list) else []:
        spec = entry.get("ItemSpecific") if isinstance(entry, dict) else None
        if isinstance(spec, dict):
            name = (spec.get("Name") or "").strip()
            val = (spec.get("Value") or "").strip()
            if name and val:
                out.append((name, val))
    return out


def _parse_categories(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return [v] if v.strip() else []
    if isinstance(v, list):
        return [c.strip() for c in v if isinstance(c, str) and c.strip()]
    return []


def build_chunk_text(p: dict) -> str:
    """Compose the plain-text representation we embed.

    Section labels (TITLE:, DESCRIPTION:, etc.) help the model understand
    structure; the bulk of the semantic signal is the description + specs.
    """
    parts: list[str] = []

    title = (p.get("Name") or "").strip()
    if title:
        parts.append(f"TITLE: {title}")
    if p.get("Brand"):    parts.append(f"BRAND: {p['Brand']}")
    if p.get("Model"):    parts.append(f"MODEL: {p['Model']}")
    if p.get("ModelNumber") and p["ModelNumber"] != p.get("Model"):
        parts.append(f"MODEL NUMBER: {p['ModelNumber']}")
    if p.get("SKU"):      parts.append(f"SKU: {p['SKU']}")

    if p.get("Subtitle"):
        parts.append(f"SUBTITLE: {p['Subtitle']}")

    desc = _strip_html(p.get("Description"))
    if desc:
        parts.append("\nDESCRIPTION:\n" + desc)

    specs = _parse_item_specifics(p.get("ItemSpecifics"))
    if specs:
        spec_lines = "\n".join(f"- {n}: {v}" for n, v in specs)
        parts.append("\nSPECIFICATIONS:\n" + spec_lines)

    cats = _parse_categories(p.get("Categories"))
    if cats:
        parts.append("\nCATEGORIES: " + " / ".join(cats))

    if p.get("product_type"):
        parts.append(f"PRODUCT TYPE: {p['product_type']}")
    if p.get("is_kitted_item") in (True, "true", "True"):
        parts.append("KITTED ITEM: yes")

    # Useful shipping context — agents get asked about freight constantly.
    ship_bits = []
    for k in ("ShippingWeight",):
        v = p.get(k)
        if v: ship_bits.append(f"{v}kg")
    dims = []
    for k in ("ShippingLength", "ShippingWidth", "ShippingHeight"):
        v = p.get(k)
        if v: dims.append(str(v))
    if len(dims) == 3:
        ship_bits.append("×".join(dims) + "m")
    if p.get("ShippingCategoryName"):
        ship_bits.append(p["ShippingCategoryName"])
    if ship_bits:
        parts.append("SHIPPING: " + ", ".join(ship_bits))

    out = "\n".join(parts)
    if len(out) > MAX_CHARS_PER_DOC:
        out = out[:MAX_CHARS_PER_DOC].rsplit(" ", 1)[0] + " …"
    return out


def build_metadata(p: dict) -> dict:
    """Surface fields the search-result UI displays. Keep this lean."""
    return {
        "brand":          p.get("Brand"),
        "model":          p.get("Model"),
        "model_number":   p.get("ModelNumber"),
        "barcode":        p.get("Barcode"),
        "categories":     _parse_categories(p.get("Categories")),
        "product_type":   p.get("product_type"),
        "is_kitted":      bool(p.get("is_kitted_item") in (True, "true", "True")),
        "shipping_weight_kg": float(p["ShippingWeight"]) if p.get("ShippingWeight") else None,
    }


def public_url(p: dict) -> str | None:
    slug = (p.get("ItemURL") or "").strip()
    if not slug:
        return None
    return f"https://www.chainsawspares.com.au/{slug}"


def parse_date(s: str | None) -> datetime | None:
    if not s or s.startswith("0000"):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_batch(model, texts: list[str]) -> list[list[float]]:
    """Embed up to EMBED_BATCH texts in one call. Retries once on transient."""
    inputs = [TextEmbeddingInput(t, task_type="RETRIEVAL_DOCUMENT") for t in texts]
    for attempt in range(2):
        try:
            embs = model.get_embeddings(inputs)
            return [e.values for e in embs]
        except Exception as exc:
            if attempt == 0:
                print(f"  embed retry after error: {exc}", file=sys.stderr)
                time.sleep(2)
                continue
            raise


# ---------------------------------------------------------------------------
# Watermark + merge
# ---------------------------------------------------------------------------

def get_watermark(bq: bigquery.Client) -> datetime | None:
    sql = f"""
    SELECT last_synced_at
    FROM `{PROJECT}.{DATASET}.refresh_state`
    WHERE source = @s LIMIT 1
    """
    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "STRING", SOURCE),
        ]),
    )
    rows = list(job.result())
    return rows[0].last_synced_at if rows else None


def set_watermark(bq: bigquery.Client, ts: datetime, rows_count: int) -> None:
    sql = f"""
    MERGE `{PROJECT}.{DATASET}.refresh_state` T
    USING (SELECT @s AS source, @t AS last_synced_at, @n AS rows_last_run, @t AS updated_at) S
    ON T.source = S.source
    WHEN MATCHED THEN UPDATE SET
      last_synced_at = S.last_synced_at,
      rows_last_run  = S.rows_last_run,
      updated_at     = S.updated_at
    WHEN NOT MATCHED THEN INSERT (source, last_synced_at, rows_last_run, updated_at)
      VALUES (S.source, S.last_synced_at, S.rows_last_run, S.updated_at)
    """
    bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("s", "STRING",   SOURCE),
        bigquery.ScalarQueryParameter("t", "TIMESTAMP", ts),
        bigquery.ScalarQueryParameter("n", "INT64",    rows_count),
    ])).result()


def merge_into_documents(bq: bigquery.Client, rows: list[dict]) -> int:
    """Upsert into kb.documents via load-into-staging-then-MERGE."""
    if not rows:
        return 0
    staging = f"_kb_stage_{uuid.uuid4().hex[:12]}"
    staging_ref = f"{PROJECT}.{DATASET}.{staging}"

    # Load staging via JSON (handles ARRAY<FLOAT64> cleanly)
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[
            bigquery.SchemaField("doc_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("source_id", "STRING"),
            bigquery.SchemaField("sku", "STRING"),
            bigquery.SchemaField("title", "STRING"),
            bigquery.SchemaField("url", "STRING"),
            bigquery.SchemaField("body", "STRING"),
            bigquery.SchemaField("metadata", "JSON"),
            bigquery.SchemaField("embedding", "FLOAT", mode="REPEATED"),
            bigquery.SchemaField("last_modified_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
        ],
    )
    bq.load_table_from_json(rows, staging_ref, job_config=job_config).result()

    sql = f"""
    MERGE `{PROJECT}.{DATASET}.documents` T
    USING `{staging_ref}` S
    ON T.doc_id = S.doc_id
    WHEN MATCHED THEN UPDATE SET
      source = S.source, source_id = S.source_id, sku = S.sku,
      title = S.title, url = S.url, body = S.body, metadata = S.metadata,
      embedding = S.embedding,
      last_modified_at = S.last_modified_at, ingested_at = S.ingested_at
    WHEN NOT MATCHED THEN INSERT ROW
    """
    try:
        bq.query(sql).result()
    finally:
        bq.delete_table(staging_ref, not_found_ok=True)
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(reset: bool = False, limit: int | None = None) -> None:
    bq = bigquery.Client(project=PROJECT)
    vertexai.init(project=PROJECT, location=LOCATION)
    embed_model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)

    if reset:
        print("--reset: clearing watermark for source 'neto_product'")
        bq.query(
            f"DELETE FROM `{PROJECT}.{DATASET}.refresh_state` WHERE source = '{SOURCE}'"
        ).result()

    watermark = get_watermark(bq)
    print(f"watermark: {watermark.isoformat() if watermark else '(none — full load)'}")

    t0 = time.perf_counter()
    products = fetch_products(bq, watermark, limit)
    print(f"fetched {len(products):,} products in {time.perf_counter()-t0:.1f}s")
    if not products:
        print("nothing to do.")
        return

    run_started_at = datetime.now(timezone.utc)
    pending: list[dict] = []
    embedded_total = 0

    # Pre-build texts once so we can bin-pack by length.
    items = [(p, build_chunk_text(p)) for p in products]

    def iter_batches():
        """Yield batches of (product, text) pairs whose total char count
        stays under MAX_CHARS_PER_BATCH so Vertex's 20k-token-per-request
        limit isn't tripped."""
        cur: list = []
        cur_chars = 0
        for p, t in items:
            tlen = len(t)
            if cur and (cur_chars + tlen > MAX_CHARS_PER_BATCH
                         or len(cur) >= EMBED_BATCH_FALLBACK):
                yield cur
                cur, cur_chars = [], 0
            cur.append((p, t))
            cur_chars += tlen
        if cur:
            yield cur

    seen = 0
    for batch in iter_batches():
        prods, texts = zip(*batch)
        t = time.perf_counter()
        vectors = embed_batch(embed_model, list(texts))
        embedded_total += len(vectors)
        seen += len(batch)
        chars = sum(len(t) for t in texts)
        print(f"  embedded {seen:>5}/{len(products):>5}  "
              f"batch={len(batch):>2}  chars={chars:>5}  "
              f"({(time.perf_counter()-t)*1000:.0f}ms)")

        for p, body, vec in zip(prods, texts, vectors):
            last_mod = parse_date(p.get("DateUpdated")) or run_started_at
            pending.append({
                "doc_id":           f"neto_product:{p['ID']}",
                "source":           SOURCE,
                "source_id":        str(p["ID"]),
                "sku":              p.get("SKU"),
                "title":            p.get("Name"),
                "url":              public_url(p),
                "body":             body,
                "metadata":         build_metadata(p),
                "embedding":        vec,
                "last_modified_at": last_mod.isoformat(),
                "ingested_at":      run_started_at.isoformat(),
            })

        if len(pending) >= MERGE_BATCH:
            n = merge_into_documents(bq, pending)
            print(f"    merged {n} rows into documents")
            pending = []

    if pending:
        n = merge_into_documents(bq, pending)
        print(f"    merged {n} rows into documents (final)")

    set_watermark(bq, run_started_at, embedded_total)
    secs = time.perf_counter() - t0
    print(f"done — {embedded_total:,} rows in {secs:.1f}s "
          f"(~${embedded_total * 1.5 / 1_000 * 0.025 / 1_000:.4f} embedding cost)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="Clear watermark before running (forces full reload)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap row count (sanity-check first runs)")
    args = parser.parse_args()
    run(reset=args.reset, limit=args.limit)
