"""Phase 1B.1 KB ingest — Neto CMS pages → Vertex AI embeddings → BQ kb.documents.

Walks the Neto ``GetContent`` API and ingests pages from the Resources
menu (product info hubs + their child manual/exploded/videos pages),
plus formal CMS pages including the returns / warranty policy. Blogs
and product-finder navigation pages are excluded per the agreed scope.

URL classification (mirrors the docs/kb-phase1-spec recon):

  *infopage              → Resources hub pages (top-level menu items)
  *manual                → product manual stub pages (PDF holders)
  *exploded              → exploded view stub pages (PDF holders)
  *videos                → curated how-to video index pages
  page/*                 → formal CMS pages (returns, warranty, etc.)
  children of any hub    → kept if body > 200 chars
  blog/*                 → EXCLUDED
  product-finder/*       → EXCLUDED
  everything else        → kept if body > 500 chars (rare)

Source tag in kb.documents is ``neto_cms``. Watermark in
``kb.refresh_state`` is keyed on ``source='neto_cms'`` so this ingest
and the product ingest don't interfere.

Usage:
    python3 scripts/kb_neto_cms_ingest.py [--reset] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery
import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

PROJECT  = "chainsawspares-385722"
DATASET  = "kb"
LOCATION = "us-central1"
SOURCE   = "neto_cms"

EMBED_MODEL          = "text-embedding-004"
MAX_CHARS_PER_DOC    = 4500
MAX_CHARS_PER_BATCH  = 35_000
EMBED_BATCH_FALLBACK = 25
MERGE_BATCH          = 500

NETO_API_URL = "https://www.chainsawspares.com.au/do/WS/NetoAPI"
NETO_USERNAME = "adil_auto_user"


# ---------------------------------------------------------------------------
# Neto API
# ---------------------------------------------------------------------------

def neto_key() -> str:
    """Pull the API key from GCP Secret Manager, falling back to env."""
    env = os.environ.get("NETO_API_KEY")
    if env:
        return env
    # Try gcloud as a fallback for local dev
    for gcloud in ("/Users/adil/google-cloud-sdk/bin/gcloud", "gcloud"):
        try:
            return subprocess.check_output(
                [gcloud, "secrets", "versions", "access", "latest",
                 "--secret", "neto-api-key", "--project", PROJECT],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            continue
    raise RuntimeError("set NETO_API_KEY env var or create secret 'neto-api-key'")


def fetch_all_content() -> list[dict]:
    """Walk GetContent paginated, return every active CMS row."""
    api_key = neto_key()
    rows: list[dict] = []
    page = 1
    while True:
        body = {
            "Filter": {
                "Page": page,
                "Limit": 200,
                "Active": "True",
                "OutputSelector": [
                    "ID", "ContentName", "ContentURL", "ContentType",
                    "ParentContentID", "DatePosted", "DateUpdated",
                    "ShortDescription1", "ShortDescription2",
                    "Description1", "Description2", "Description3",
                    "Label1", "Label2", "Label3",
                    "SEOMetaDescription", "SEOPageHeading",
                ],
            },
        }
        req = urllib.request.Request(
            NETO_API_URL,
            data=json.dumps(body).encode(),
            headers={
                "NETOAPI_ACTION":   "GetContent",
                "NETOAPI_USERNAME": NETO_USERNAME,
                "NETOAPI_KEY":      api_key,
                "Content-Type":     "application/json",
                "Accept":           "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        if d.get("Ack") != "Success":
            raise RuntimeError(f"Neto API error on page {page}: {d}")
        chunk = d.get("Content", []) or []
        sys.stderr.write(f"  page {page}: {len(chunk)} rows\n")
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 200:
            break
        page += 1
    return rows


# ---------------------------------------------------------------------------
# Filtering — what's in scope for the KB
# ---------------------------------------------------------------------------

def classify(url: str) -> str:
    u = (url or "").lower()
    if not u:                                     return "noop"
    if u.startswith("blog/") or "/blog/" in u:    return "blog"
    if u.startswith("product-finder/"):           return "navigation"
    if u.endswith("manual"):                      return "manual"
    if u.endswith("exploded"):                    return "exploded"
    if u.endswith("infopage"):                    return "infopage_hub"
    if u.endswith("videos"):                      return "videos"
    if u.startswith("page/"):                     return "cms_page"
    return "other"


def body_chars(row: dict) -> int:
    return sum(len((row.get(k) or "")) for k in (
        "ShortDescription1", "ShortDescription2",
        "Description1", "Description2", "Description3",
        "Label1", "Label2", "Label3",
    ))


def select_in_scope(rows: list[dict]) -> list[dict]:
    """Drop blogs + navigation + empty rows; keep substantive children
    of any Resources hub even if their URL doesn't follow the *infopage
    family naming convention."""
    keep: list[dict] = []

    by_id = {str(r.get("ID")): r for r in rows}
    hub_ids = {str(r.get("ID")) for r in rows
               if classify(r.get("ContentURL", "")) == "infopage_hub"}

    for r in rows:
        bucket = classify(r.get("ContentURL", ""))
        if bucket in ("blog", "navigation", "noop"):
            continue
        # First-class kept buckets — always in
        if bucket in ("infopage_hub", "manual", "exploded", "videos", "cms_page"):
            keep.append(r)
            continue
        # Hub children — keep if substantive
        parent = str(r.get("ParentContentID") or "")
        if parent in hub_ids and body_chars(r) > 200:
            keep.append(r)
            continue
        # Other long-form pages — keep if there's real content
        if bucket == "other" and body_chars(r) > 500:
            keep.append(r)
    return keep


# ---------------------------------------------------------------------------
# Chunk text
# ---------------------------------------------------------------------------

_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE   = re.compile(r"\s+")


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = _HTML_RE.sub(" ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return _WS_RE.sub(" ", s).strip()


def build_chunk_text(r: dict, parent: dict | None) -> str:
    """Compose the plain-text representation we embed.

    Section labels help the model understand structure. Same pattern as
    the products ingest so retrieval treats sources uniformly.
    """
    parts: list[str] = []

    title = (r.get("ContentName") or "").strip()
    if title:
        parts.append(f"TITLE: {title}")
    bucket = classify(r.get("ContentURL", ""))
    parts.append(f"PAGE TYPE: {bucket}")
    if parent:
        parts.append(f"PARENT: {parent.get('ContentName')}")

    # SEO heading + meta — often the cleanest summary
    seo_heading = (r.get("SEOPageHeading") or "").strip()
    if seo_heading and seo_heading != title:
        parts.append(f"HEADING: {seo_heading}")
    seo_meta = (r.get("SEOMetaDescription") or "").strip()
    if seo_meta:
        parts.append(f"SUMMARY: {seo_meta}")

    # Short descriptions
    for k in ("ShortDescription1", "ShortDescription2"):
        v = _strip_html(r.get(k))
        if v:
            parts.append(v)

    # Body fields
    body_lines: list[str] = []
    for k in ("Description1", "Description2", "Description3"):
        v = _strip_html(r.get(k))
        if v:
            body_lines.append(v)
    if body_lines:
        parts.append("\nBODY:\n" + "\n\n".join(body_lines))

    # Section labels (CMS designer sometimes puts useful copy here)
    label_lines: list[str] = []
    for k in ("Label1", "Label2", "Label3"):
        v = _strip_html(r.get(k))
        if v:
            label_lines.append(v)
    if label_lines:
        parts.append("\nLABELS:\n" + " · ".join(label_lines))

    out = "\n".join(parts)
    if len(out) > MAX_CHARS_PER_DOC:
        out = out[:MAX_CHARS_PER_DOC].rsplit(" ", 1)[0] + " …"
    return out


def build_metadata(r: dict, parent: dict | None) -> dict:
    return {
        "content_type":  classify(r.get("ContentURL", "")),
        "content_name":  r.get("ContentName"),
        "parent_id":     r.get("ParentContentID"),
        "parent_name":   parent.get("ContentName") if parent else None,
        "neto_content_id": r.get("ID"),
    }


def public_url(r: dict) -> str | None:
    slug = (r.get("ContentURL") or "").strip()
    return f"https://www.chainsawspares.com.au/{slug}" if slug else None


def parse_date(s: str | None) -> datetime | None:
    if not s or s.startswith("0000"):
        return None
    # Neto returns "YYYY-MM-DD HH:MM:SS" UTC
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Embedding + watermark + merge (same shape as kb_neto_products_ingest)
# ---------------------------------------------------------------------------

def embed_batch(model, texts: list[str]) -> list[list[float]]:
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


def get_watermark(bq) -> datetime | None:
    rows = list(bq.query(
        f"SELECT last_synced_at FROM `{PROJECT}.{DATASET}.refresh_state` WHERE source = @s LIMIT 1",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "STRING", SOURCE),
        ]),
    ).result())
    return rows[0].last_synced_at if rows else None


def set_watermark(bq, ts: datetime, rows_count: int) -> None:
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


def merge_into_documents(bq, rows: list[dict]) -> int:
    if not rows:
        return 0
    staging = f"_kb_cms_stage_{uuid.uuid4().hex[:12]}"
    staging_ref = f"{PROJECT}.{DATASET}.{staging}"

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
        print("--reset: clearing watermark for source 'neto_cms'")
        bq.query(
            f"DELETE FROM `{PROJECT}.{DATASET}.refresh_state` WHERE source = '{SOURCE}'"
        ).result()
        bq.query(
            f"DELETE FROM `{PROJECT}.{DATASET}.documents` WHERE source = '{SOURCE}'"
        ).result()

    watermark = get_watermark(bq)
    print(f"watermark: {watermark.isoformat() if watermark else '(none — full load)'}")

    t0 = time.perf_counter()
    print("fetching CMS rows from Neto…")
    all_rows = fetch_all_content()
    print(f"  fetched {len(all_rows):,} active CMS rows total")

    in_scope = select_in_scope(all_rows)
    print(f"  {len(in_scope):,} rows in scope after filtering")

    # Incremental — drop rows whose DateUpdated isn't newer than the watermark
    if watermark:
        before = len(in_scope)
        in_scope = [
            r for r in in_scope
            if (d := parse_date(r.get("DateUpdated"))) is not None and d > watermark
        ]
        print(f"  watermark filter: {before:,} → {len(in_scope):,} changed since last run")

    if limit:
        in_scope = in_scope[:limit]
        print(f"  --limit {limit}: {len(in_scope):,} rows after cap")

    if not in_scope:
        print("nothing to do.")
        return

    by_id = {str(r.get("ID")): r for r in all_rows}
    run_started_at = datetime.now(timezone.utc)
    items: list[tuple[dict, str]] = []
    for r in in_scope:
        parent = by_id.get(str(r.get("ParentContentID") or ""))
        items.append((r, build_chunk_text(r, parent)))

    def iter_batches():
        cur: list = []
        cur_chars = 0
        for r, t in items:
            tlen = len(t)
            if cur and (cur_chars + tlen > MAX_CHARS_PER_BATCH
                        or len(cur) >= EMBED_BATCH_FALLBACK):
                yield cur
                cur, cur_chars = [], 0
            cur.append((r, t))
            cur_chars += tlen
        if cur:
            yield cur

    pending: list[dict] = []
    embedded_total = 0
    seen = 0
    for batch in iter_batches():
        rs, texts = zip(*batch)
        t = time.perf_counter()
        vectors = embed_batch(embed_model, list(texts))
        embedded_total += len(vectors)
        seen += len(batch)
        chars = sum(len(t) for t in texts)
        print(f"  embedded {seen:>5}/{len(items):>5}  batch={len(batch):>2}  "
              f"chars={chars:>5}  ({(time.perf_counter()-t)*1000:.0f}ms)")

        for r, body, vec in zip(rs, texts, vectors):
            parent = by_id.get(str(r.get("ParentContentID") or ""))
            last_mod = parse_date(r.get("DateUpdated")) or run_started_at
            pending.append({
                "doc_id":           f"neto_cms:{r['ID']}",
                "source":           SOURCE,
                "source_id":        str(r["ID"]),
                "sku":              None,
                "title":            r.get("ContentName"),
                "url":              public_url(r),
                "body":             body,
                "metadata":         build_metadata(r, parent),
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
    print(f"done — {embedded_total:,} rows in {secs:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="Clear watermark + existing neto_cms rows before loading")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap row count (handy for first sanity checks)")
    args = parser.parse_args()
    run(reset=args.reset, limit=args.limit)
