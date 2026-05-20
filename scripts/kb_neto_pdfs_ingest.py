"""Phase 1B.2 KB ingest — brochures + technical manuals (PDFs) → kb.documents.

Walks the Neto ``GetContent`` API for ``*manual`` / ``*exploded`` /
``*infopage`` / ``page/*`` rows, extracts ``<a href="...pdf">`` links
from their HTML bodies, downloads each unique PDF, extracts text with
``pypdf``, chunks into ~1,000-char overlapping windows, embeds via
Vertex ``text-embedding-004``, and merges into ``kb.documents``.

Source tag: ``neto_pdf``. Doc IDs: ``neto_pdf:{sha256(url)[:12]}:c{idx}``
so each chunk is stable (re-ingests overwrite same row, no duplicates).

Phase 1B.2 scope:
* Text-extractable PDFs only. Scanned image-only PDFs (where pypdf
  returns near-zero characters) are logged + skipped — OCR is a
  separate concern for a later phase if needed.
* No watermark filter: we re-download every PDF on each run. The PDFs
  rarely change; cron firing keeps things current. The MERGE on
  doc_id makes re-ingest a no-op for unchanged content.

Usage:
    python3 scripts/kb_neto_pdfs_ingest.py [--reset] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

from google.cloud import bigquery
import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

PROJECT  = "chainsawspares-385722"
DATASET  = "kb"
LOCATION = "us-central1"
SOURCE   = "neto_pdf"

# Document AI OCR fallback for image-only (scanned) PDFs. Created
# 2026-05-21 in the chainsawspares-385722 project, us location.
# The pypdf path can't read scanned PDFs (no text layer), so for the
# 24 manufacturer manuals (VS135ES, VS175ES, JPE680/750/1000, Perla
# Barb 84cc / 91.6cc, EF-81/82 fence energisers, PBKK brushcutter,
# JWP50/80 wood splitters, LT-80 log horse, etc.) we send the blob
# to Document AI's OCR processor and use the returned text.
#
# Sync ``process_document`` supports up to 15 pages per request and
# 20 MB per file. Longer PDFs are split via pypdf into 15-page chunks
# and each chunk OCR'd separately, then concatenated.
#
# Cost: $1.50 per 1,000 pages. The 24 known scans total ~240 pages,
# so the one-off backfill is well under $1. Going forward, only new
# scanned brochures hit OCR (already-known URLs are filtered by the
# incremental hash check).
DOCAI_PROCESSOR_NAME = (
    "projects/34820011125/locations/us/processors/eb4d1250b17c937e"
)
DOCAI_API_ENDPOINT     = "us-documentai.googleapis.com"
DOCAI_SYNC_PAGE_LIMIT  = 15

EMBED_MODEL          = "text-embedding-004"
MAX_CHARS_PER_DOC    = 4500
MAX_CHARS_PER_BATCH  = 35_000
EMBED_BATCH_FALLBACK = 25
MERGE_BATCH          = 500

# Chunking — overlap helps retrieval catch spec tables / instructions
# that straddle a boundary.
CHUNK_TARGET_CHARS = 1100
CHUNK_OVERLAP      = 200

NETO_API_URL  = "https://www.chainsawspares.com.au/do/WS/NetoAPI"
NETO_USERNAME = "adil_auto_user"

# Some PDFs are linked via http:// in old CMS pages — rewrite to https://
# at ingest time so we don't blow downloads on protocol issues.
HTTP_REWRITE = re.compile(r"^http://(www\.)?chainsawspares\.com\.au/")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Neto API helpers (mirror kb_neto_cms_ingest)
# ---------------------------------------------------------------------------

def neto_key() -> str:
    env = os.environ.get("NETO_API_KEY")
    if env:
        return env
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
    api_key = neto_key()
    rows: list[dict] = []
    page = 1
    while True:
        body = {"Filter": {
            "Page": page, "Limit": 200, "Active": "True",
            "OutputSelector": ["ID","ContentName","ContentURL","ContentType",
                               "ParentContentID","DateUpdated",
                               "Description1","Description2","Description3"],
        }}
        req = urllib.request.Request(NETO_API_URL,
            data=json.dumps(body).encode(),
            headers={"NETOAPI_ACTION":"GetContent","NETOAPI_USERNAME":NETO_USERNAME,
                     "NETOAPI_KEY":api_key,"Content-Type":"application/json",
                     "Accept":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        if d.get("Ack") != "Success":
            raise RuntimeError(f"Neto API error page {page}: {d}")
        chunk = d.get("Content", []) or []
        sys.stderr.write(f"  page {page}: {len(chunk)} rows\n")
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < 200: break
        page += 1
    return rows


# ---------------------------------------------------------------------------
# Find PDF URLs across the CMS corpus
# ---------------------------------------------------------------------------

PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)


def collect_pdf_links(rows: list[dict]) -> dict[str, list[dict]]:
    """Return ``{pdf_url: [parent_cms_row, ...]}`` — parents are kept so we
    can store context in each chunk's metadata."""
    pdfs: dict[str, list[dict]] = {}
    for r in rows:
        text = " ".join((r.get(k) or "") for k in ("Description1","Description2","Description3"))
        if not text: continue
        for m in PDF_HREF_RE.finditer(text):
            url = m.group(1).strip()
            # Make absolute if relative
            if url.startswith("/"):
                url = "https://www.chainsawspares.com.au" + url
            url = HTTP_REWRITE.sub("https://www.chainsawspares.com.au/", url)
            pdfs.setdefault(url, []).append(r)
    return pdfs


# ---------------------------------------------------------------------------
# PDF download + text extraction
# ---------------------------------------------------------------------------

def download_pdf(url: str) -> bytes | None:
    """GET the PDF, return bytes. Returns None on any HTTP error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chainsaw-ops-kb/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            if len(data) < 200:
                return None
            return data
    except Exception as exc:
        log.warning("PDF download failed %s: %s", url, exc)
        return None


def ocr_pdf(blob: bytes) -> list[str]:
    """OCR a scanned PDF via Document AI; return per-page text strings.

    Used as the fallback path when ``extract_text_pages`` returns
    nothing useful (image-only PDFs that ``looks_like_scan`` flagged).

    Sync ``process_document`` caps at 15 pages per call. For longer
    documents we split via pypdf into 15-page slices and stitch the
    per-slice page lists back together. The text returned for each
    page is built from the layout text-anchor segments — same shape
    as ``extract_text_pages`` so the rest of the pipeline (chunker
    + embedder + MERGE) doesn't need to know which path produced it.

    Raises on hard API failures (network, IAM, quota) so the caller
    can log + skip the specific URL rather than abort the whole run.
    """
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    from google.cloud import documentai_v1 as documentai

    client = documentai.DocumentProcessorServiceClient(
        client_options={"api_endpoint": DOCAI_API_ENDPOINT}
    )

    def _ocr_chunk(chunk_blob: bytes) -> list[str]:
        req = documentai.ProcessRequest(
            name=DOCAI_PROCESSOR_NAME,
            raw_document=documentai.RawDocument(
                content=chunk_blob, mime_type="application/pdf"
            ),
        )
        result = client.process_document(request=req)
        doc = result.document
        out: list[str] = []
        for page in doc.pages:
            parts: list[str] = []
            if page.layout and page.layout.text_anchor:
                for seg in page.layout.text_anchor.text_segments:
                    parts.append(doc.text[int(seg.start_index):int(seg.end_index)])
            out.append("".join(parts))
        return out

    # Fast path — single sync call for PDFs at or below the page cap.
    reader = PdfReader(BytesIO(blob), strict=False)
    n_pages = len(reader.pages)
    if n_pages <= DOCAI_SYNC_PAGE_LIMIT:
        return _ocr_chunk(blob)

    # Slow path — split the PDF into 15-page slices, OCR each, stitch.
    pages_text: list[str] = []
    for start in range(0, n_pages, DOCAI_SYNC_PAGE_LIMIT):
        end = min(start + DOCAI_SYNC_PAGE_LIMIT, n_pages)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = BytesIO()
        writer.write(buf)
        pages_text.extend(_ocr_chunk(buf.getvalue()))
    return pages_text


def extract_text_pages(blob: bytes) -> list[str]:
    """Return one extracted text string per PDF page."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(blob), strict=False)
    except Exception as exc:
        log.warning("PdfReader failed: %s", exc)
        return []
    pages: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        pages.append(t)
    return pages


def looks_like_scan(pages: list[str], blob_size: int) -> bool:
    """A scanned (image-only) PDF gives almost no extracted text per MB."""
    total_chars = sum(len(p) for p in pages)
    mb = max(1, blob_size / 1_048_576)
    return total_chars / mb < 80  # heuristic: real text PDFs hit 1000s of chars/MB


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def clean_page(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def chunk_pdf(pages: list[str]) -> list[dict]:
    """Build (page_num, chunk_idx, text) chunks across all pages.

    Strategy: build a single normalised text stream (with page markers),
    then chunk by char count with overlap. We track which page each
    chunk started on so citations can deep-link to the right page.
    """
    chunks: list[dict] = []
    chunk_idx = 0

    # Build a single stream with page markers so we know where each chunk lives.
    stream_parts = []
    stream_pages = []  # parallel: page_num per char span (start, end, page_num)
    cursor = 0
    for i, raw in enumerate(pages, start=1):
        t = clean_page(raw)
        if not t: continue
        if stream_parts:
            stream_parts.append("\n")
            cursor += 1
        start = cursor
        stream_parts.append(t)
        cursor += len(t)
        stream_pages.append((start, cursor, i))

    stream = "".join(stream_parts)
    if not stream.strip():
        return []

    def page_for_offset(off: int) -> int:
        for s, e, p in stream_pages:
            if s <= off < e:
                return p
        return stream_pages[-1][2] if stream_pages else 1

    pos = 0
    while pos < len(stream):
        end = min(pos + CHUNK_TARGET_CHARS, len(stream))
        # Try to end on a sentence boundary for cleaner chunks.
        if end < len(stream):
            for stopper in (". ", "\n", " "):
                idx = stream.rfind(stopper, pos + CHUNK_TARGET_CHARS // 2, end)
                if idx > 0:
                    end = idx + len(stopper)
                    break
        text = stream[pos:end].strip()
        if text:
            chunks.append({
                "chunk_idx": chunk_idx,
                "page_num": page_for_offset(pos),
                "text": text,
            })
            chunk_idx += 1
        if end >= len(stream):
            break
        pos = max(end - CHUNK_OVERLAP, pos + 1)

    return chunks


# ---------------------------------------------------------------------------
# Embedding + merge (mirrors product/CMS ingests)
# ---------------------------------------------------------------------------

def embed_batch(model, texts: list[str]) -> list[list[float]]:
    inputs = [TextEmbeddingInput(t, task_type="RETRIEVAL_DOCUMENT") for t in texts]
    for attempt in range(2):
        try:
            return [e.values for e in model.get_embeddings(inputs)]
        except Exception as exc:
            if attempt == 0:
                print(f"  embed retry after error: {exc}", file=sys.stderr)
                time.sleep(2)
                continue
            raise


def merge_into_documents(bq, rows: list[dict]) -> int:
    if not rows: return 0
    staging = f"_kb_pdf_stage_{uuid.uuid4().hex[:12]}"
    ref = f"{PROJECT}.{DATASET}.{staging}"
    cfg = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[
            bigquery.SchemaField("doc_id","STRING",mode="REQUIRED"),
            bigquery.SchemaField("source","STRING",mode="REQUIRED"),
            bigquery.SchemaField("source_id","STRING"),
            bigquery.SchemaField("sku","STRING"),
            bigquery.SchemaField("title","STRING"),
            bigquery.SchemaField("url","STRING"),
            bigquery.SchemaField("body","STRING"),
            bigquery.SchemaField("metadata","JSON"),
            bigquery.SchemaField("embedding","FLOAT",mode="REPEATED"),
            bigquery.SchemaField("last_modified_at","TIMESTAMP",mode="REQUIRED"),
            bigquery.SchemaField("ingested_at","TIMESTAMP",mode="REQUIRED"),
        ],
    )
    bq.load_table_from_json(rows, ref, job_config=cfg).result()
    sql = f"""
    MERGE `{PROJECT}.{DATASET}.documents` T
    USING `{ref}` S
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
        bq.delete_table(ref, not_found_ok=True)
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(reset: bool = False, limit: int | None = None, dry_run: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    bq = bigquery.Client(project=PROJECT)
    vertexai.init(project=PROJECT, location=LOCATION)
    embed_model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)

    if reset and not dry_run:
        print("--reset: clearing existing neto_pdf rows + watermark")
        bq.query(f"DELETE FROM `{PROJECT}.{DATASET}.documents` WHERE source = '{SOURCE}'").result()
        bq.query(f"DELETE FROM `{PROJECT}.{DATASET}.refresh_state` WHERE source = '{SOURCE}'").result()

    print("fetching CMS rows from Neto…")
    all_rows = fetch_all_content()
    print(f"  fetched {len(all_rows):,} active CMS rows")

    pdfs = collect_pdf_links(all_rows)
    print(f"  found {len(pdfs):,} unique PDF URLs linked from CMS")

    # --- Incremental filter -------------------------------------------------
    # Previously this script bailed out entirely if any neto_pdf rows
    # existed in kb.documents — a deliberate guard to avoid re-embedding the
    # same brochures every hour. Cheap on the wrong side: it also meant
    # **new** PDFs added to the website after the initial load never got
    # picked up. Now we ask BQ which URL-hashes we already have and only
    # process the ones we don't. The doc_id pattern is
    # ``neto_pdf:{sha256(url)[:12]}:c{chunk_idx}``, so ``source_id``
    # (the url_hash) is the natural per-URL key.
    if not reset and not dry_run and pdfs:
        existing_hashes = {
            row.source_id for row in bq.query(
                f"SELECT DISTINCT source_id "
                f"FROM `{PROJECT}.{DATASET}.documents` "
                f"WHERE source = '{SOURCE}' AND source_id IS NOT NULL"
            ).result()
        }
        before = len(pdfs)
        pdfs = {
            url: parents for url, parents in pdfs.items()
            if hashlib.sha256(url.encode()).hexdigest()[:12] not in existing_hashes
        }
        skipped = before - len(pdfs)
        print(f"  incremental filter: {skipped:,} already ingested, {len(pdfs):,} new to process")
        if not pdfs:
            print("  no new PDFs — nothing to embed. Done.")
            return

    if limit:
        pdfs = dict(list(pdfs.items())[:limit])
        print(f"  --limit {limit}: keeping {len(pdfs):,} PDFs")

    if dry_run:
        for url, parents in pdfs.items():
            print(f"  {url}  ← {parents[0].get('ContentName')[:60] if parents else ''}")
        return

    run_started_at = datetime.now(timezone.utc)
    skipped_scans = []
    chunks_to_embed: list[dict] = []  # each: {url, parents, chunk}
    t_total = time.perf_counter()

    ocr_failed: list[str] = []  # OCR was attempted but failed (network/quota)
    for i, (url, parents) in enumerate(pdfs.items(), 1):
        t = time.perf_counter()
        blob = download_pdf(url)
        if blob is None:
            print(f"  [{i}/{len(pdfs)}] SKIP (download failed) {url}")
            continue
        pages = extract_text_pages(blob)
        if not pages:
            print(f"  [{i}/{len(pdfs)}] SKIP (pdf parse failed) {url}")
            continue
        # If the PDF is image-only (no embedded text), fall back to
        # Document AI OCR instead of skipping. Tracked via ``ocr_used``
        # so the per-chunk metadata can record the provenance.
        ocr_used = False
        if looks_like_scan(pages, len(blob)):
            try:
                ocr_pages = ocr_pdf(blob)
            except Exception as exc:
                ocr_failed.append(url)
                print(f"  [{i}/{len(pdfs)}] OCR FAILED ({exc}) {url}")
                continue
            if not ocr_pages or sum(len(p) for p in ocr_pages) < 200:
                # OCR ran but returned essentially nothing — treat as
                # un-OCR-able (e.g. blank scans, diagrams only). Skip
                # rather than embed empty chunks.
                skipped_scans.append(url)
                print(f"  [{i}/{len(pdfs)}] SKIP (OCR returned no text) {url}")
                continue
            pages = ocr_pages
            ocr_used = True
        ch = chunk_pdf(pages)
        if not ch:
            print(f"  [{i}/{len(pdfs)}] SKIP (no text after chunk) {url}")
            continue
        for c in ch:
            chunks_to_embed.append({"url": url, "parents": parents, "chunk": c,
                                    "n_pages": len(pages), "blob_size": len(blob),
                                    "ocr_used": ocr_used})
        tag = "OCR" if ocr_used else "txt"
        print(f"  [{i}/{len(pdfs)}] [{tag}] {url[-60:]:<60}  pages={len(pages):>3} chunks={len(ch):>3}  ({(time.perf_counter()-t):.1f}s)")

    if skipped_scans:
        print(f"\n  scanned PDFs with no extractable text (even after OCR): {len(skipped_scans)}")
    if ocr_failed:
        print(f"  OCR call failed for: {len(ocr_failed)} URL(s) — they'll retry next run")
    print(f"\n  embedding {len(chunks_to_embed):,} chunks…")

    def iter_batches():
        cur, cur_chars = [], 0
        for c in chunks_to_embed:
            t = c["chunk"]["text"]
            if len(t) > MAX_CHARS_PER_DOC:
                t = t[:MAX_CHARS_PER_DOC].rsplit(" ",1)[0] + " …"
                c["chunk"]["text"] = t
            tlen = len(t)
            if cur and (cur_chars + tlen > MAX_CHARS_PER_BATCH
                        or len(cur) >= EMBED_BATCH_FALLBACK):
                yield cur
                cur, cur_chars = [], 0
            cur.append(c)
            cur_chars += tlen
        if cur:
            yield cur

    pending: list[dict] = []
    embedded_total = 0
    seen = 0
    for batch in iter_batches():
        texts = [c["chunk"]["text"] for c in batch]
        t = time.perf_counter()
        vectors = embed_batch(embed_model, texts)
        embedded_total += len(vectors)
        seen += len(batch)
        chars = sum(len(t) for t in texts)
        print(f"    embedded {seen:>5}/{len(chunks_to_embed):>5}  batch={len(batch):>2}  chars={chars:>5}  ({(time.perf_counter()-t)*1000:.0f}ms)")

        for c, vec in zip(batch, vectors):
            url = c["url"]
            url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
            chunk = c["chunk"]
            primary_parent = c["parents"][0] if c["parents"] else {}
            pdf_filename = url.rsplit("/", 1)[-1]
            display_title = primary_parent.get("ContentName") or pdf_filename
            pending.append({
                "doc_id":           f"neto_pdf:{url_hash}:c{chunk['chunk_idx']}",
                "source":           SOURCE,
                "source_id":        url_hash,
                "sku":              None,
                "title":            display_title,
                "url":              url,
                "body":             chunk["text"],
                "metadata":         {
                    "pdf_url": url,
                    "pdf_filename": pdf_filename,
                    "n_pages": c["n_pages"],
                    "page_num": chunk["page_num"],
                    "chunk_idx": chunk["chunk_idx"],
                    # Provenance: True if the text came from Document AI
                    # OCR (scanned PDF), False if it came from pypdf
                    # (PDF had an embedded text layer). Useful for the
                    # rare case where OCR quality is poor and we want
                    # to filter or re-process specific chunks later.
                    "ocr_used": bool(c.get("ocr_used")),
                    "parent_cms_name": primary_parent.get("ContentName"),
                    "parent_cms_url":  ("https://www.chainsawspares.com.au/"
                                        + (primary_parent.get("ContentURL") or "")
                                       ) if primary_parent.get("ContentURL") else None,
                    "parent_cms_id":   primary_parent.get("ID"),
                },
                "embedding":        vec,
                "last_modified_at": run_started_at.isoformat(),
                "ingested_at":      run_started_at.isoformat(),
            })

        if len(pending) >= MERGE_BATCH:
            n = merge_into_documents(bq, pending)
            print(f"      merged {n} rows into documents")
            pending = []

    if pending:
        n = merge_into_documents(bq, pending)
        print(f"      merged {n} rows into documents (final)")

    # Update watermark — for PDFs this is just "last successful run"
    bq.query(f"""
    MERGE `{PROJECT}.{DATASET}.refresh_state` T
    USING (SELECT @s AS source, @t AS last_synced_at, @n AS rows_last_run, @t AS updated_at) S
    ON T.source = S.source
    WHEN MATCHED THEN UPDATE SET last_synced_at=S.last_synced_at, rows_last_run=S.rows_last_run, updated_at=S.updated_at
    WHEN NOT MATCHED THEN INSERT (source, last_synced_at, rows_last_run, updated_at)
      VALUES (S.source, S.last_synced_at, S.rows_last_run, S.updated_at)
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("s","STRING",SOURCE),
        bigquery.ScalarQueryParameter("t","TIMESTAMP",run_started_at),
        bigquery.ScalarQueryParameter("n","INT64",embedded_total),
    ])).result()

    secs = time.perf_counter() - t_total
    print(f"\ndone — {embedded_total:,} chunks in {secs:.1f}s · {len(skipped_scans)} scans skipped")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reset",   action="store_true")
    p.add_argument("--limit",   type=int)
    p.add_argument("--dry-run", action="store_true", help="List PDF URLs without downloading")
    a = p.parse_args()
    run(reset=a.reset, limit=a.limit, dry_run=a.dry_run)
