# Knowledge Base — Phase 1 implementation spec

A concrete, actionable plan for ingesting curated SharePoint + Dataform
content into a vector store and exposing it as a search API on the
customer card. Written so that whoever picks this up — Adil, me, a
future contractor — can start cracking without redoing reconnaissance.

**Companion docs (read first):**
- [`sharepoint-inventory.md`](sharepoint-inventory.md) — full tenant inventory
- [`sharepoint-cs-drill.md`](sharepoint-cs-drill.md) — Customer Service site files
- [`sharepoint-deep-drill.md`](sharepoint-deep-drill.md) — bigger sites summary
- [`sharepoint-subtree-drill.md`](sharepoint-subtree-drill.md) — Training + CS Team folders

**Dependencies that already exist:**
- ✅ Azure AD app registered (`30ee98d1-7ccc-4315-a1f4-01ce96229962`)
- ✅ Microsoft Graph credentials in GCP Secret Manager
  (`sharepoint-tenant-id`, `sharepoint-client-id`, `sharepoint-client-secret`)
- ✅ BigQuery project `chainsawspares-385722` with billing
- ✅ Vertex AI enabled (used by `chainsaw-call-analyzer`)

---

## 1. Goal

Build a searchable knowledge base over chainsawspares' product manuals,
SOPs, policies, and reference docs. Surface it as:

- **A search box on the Customer 360 card**: agent types a question
  during a call, gets an answer with citations.
- **A `/api/kb/search` endpoint** that any future feature
  (live-transcript copilot, automated email replies) can call.

Out-of-scope for Phase 1: document refresh automation, video/image
content, multi-tenant access control, agent-curation UX.

---

## 2. Source inventory — what we ingest

All paths are inside the Microsoft 365 tenant `jonoandjohno.sharepoint.com`.
Library = top-level container; sub-folder is what we walk under it.

### 2a. SharePoint sources

| # | Site | Library | Sub-folder | Files | Why |
|--:|---|---|---|--:|---|
| 1 | `/sites/OnlineCustomerServiceTeam859` | `Procedures` | (root) | 30 | The team's master SOP set — Neto/RC/RMA processes |
| 2 | `/sites/OnlineCustomerServiceTeam859` | `Documents` | `Product Documents/` | 19 | Engine manuals, exploded parts diagrams (PDFs) |
| 3 | `/sites/OnlineCustomerServiceTeam859` | `Documents` | `Ring Central and Contact Centre/` | 3 | Phone-system SOPs |
| 4 | `/sites/OnlineCustomerServiceTeam859` | `Awaiting management sign off` | (root) | 3 | New pump/return procedures (incl. Pump Troubleshooting) |
| 5 | Root site (`https://jonoandjohno.sharepoint.com`) | `Online CS` | `Customer Service Team/Products/` | ~1,127 | **Per-product knowledge tree, hand-organised** ⭐ |
| 6 | Root site | `Online CS` | `Customer Service Team/Policies and Procedures/` | 73 | Policy docs |
| 7 | Root site | `Online CS` | `Customer Service Team/SOP's/` | 16 | More SOPs |
| 8 | Root site | `Online CS` | `Customer Service Team/Email Templates/` | 6 | Canned email replies |
| 9 | Root site | `Online CS` | `Customer Service Team/Reference materials/` | 17 | Bars catalogue, returns reference |
| 10 | Root site | `Online CS` | `Customer Service Team/Video Tutorial Links/` | 40 | Curated index of video tutorials |
| 11 | Root site | `Online CS` | `Staff site - Training (draft)/Training Course/` | ~16 | Chainsaw training session plans |
| 12 | Root site | `Online CS` | `Staff site - Training (draft)/Instruction Manuals & Reference Material/` | ~16 | Bars/chains/instruction PDFs |
| 13 | Root site | `Online CS` | `Staff site - Training (draft)/Warranty/` (text only) | ~50 | Warranty issue write-ups (skip the photos) |
| 14 | `/sites/Admin` | `Daily Operations` | (PDFs only) | ~1,588 | Operational SOPs |
| 15 | `/sites/Admin` | `Marketing` | (text only) | ~25 | Brochures, brand-relevant doc |
| 16 | `/sites/JonoJohno-allstaff` | `Documents` | (text only) | ~370 | Mixed — staff comms, training |
| 17 | `/sites/CustomerServiceToolBox` | `Documents` | (root) | 1 | Customer Service Toolbox Notes |

**Total for §2a: ~3,400 SharePoint files, ~3.5 GB after filtering out video/image/binary.**

The full Phase 1 corpus combines this with §2b (Dataform product
catalogue, ~6,000 chunks) and §2c (website brochure PDFs, ~33 PDFs).
Three layers of authority:

| Layer | Source | What it gives the agent |
|---|---|---|
| Internal | SharePoint procedures, training, CS team docs | "How JJ does things" — internal SOPs and tribal knowledge |
| Catalogue | `neto_product_list.Description` + specifics | "What the website tells customers" — same text the customer is reading on the product page |
| Authoritative | Website brochure PDFs + **exploded-parts diagrams** (~93 PDFs, all referenced from CMS pages) | "The manual" — published product documentation including exploded views for parts identification |
| Editorial | All Neto CMS pages via `GetContent` API — blog, brand pages, category descriptions, FAQs, policies, About, info pages (~156 substantial rows) | "How to choose / why is X happening / what's the policy on Y" — buying guides, troubleshooting walkthroughs, hand-written brand and category overviews, store policies |

When an agent searches, retrieval should ideally pull a chunk from each
layer for a balanced answer.

### 2b. Dataform / BigQuery sources — `neto_product_list` (the website's catalogue)

The Neto product list IS the website. Its `Description` field powers the
"Description" tab on every product page, and a few sister fields powered
the Warranty/Specifications tabs (often empty in practice — the website
either suppresses the tab or renders boilerplate when the field is blank).

For the KB, treat each product as one chunkable document. The text we
ingest is the **stripped-HTML concatenation** of these fields, in this
order, separated by section headings:

```
{SKU} · {Brand} · {Name}

{ShortDescription if any}
{stripped(Description)}

Features
{stripped(Features)} (typically empty)

Warranty
{stripped(Warranty)} (typically empty)

Specifications
{stripped(Specifications)} (typically empty)

Specifics
- {ItemSpecifics[].Name}: {ItemSpecifics[].Value}        ← parsed from JSON
- ...

Custom content
{stripped(CustomContent)} (typically empty)

Search keywords
{SearchKeywords}                                          ← explicit alt-names

SEO summary
{SEOMetaDescription}                                       ← concise human-friendly summary
```

| # | Source | Treatment | Why |
|--:|---|---|---|
| 18 | `dataform.neto_product_list` (text fields) | One chunk per row, formatted as above. Strip HTML tags from `Description` (it's stored as HTML — `bleach.clean(strip=True)` or `html2text`). Skip rows where the joined text is < 40 chars. | The 6k-product catalogue itself. Search "Honda HRU216 blade" or "62cc post hole digger" returns the SKU instantly. |
| 19 | `dataform.neto_product_list.ItemSpecifics` (JSON) | Already merged into chunk #18 as bullet list. Don't double-ingest. | Structured specs (Type, Material, Compatibility, etc.) |

**Important caveats for ingestion**:
- Filter out junk: a few rows have `Description` lengths in the millions of characters (HTML pollution from copy-paste spam). Cap chunk text at 20 KB per product; truncate cleanly with a "[truncated]" marker.
- Active filter: include both active and inactive products initially — agents take calls about discontinued items too. Tag chunks with `is_active: bool` so we can later boost active products in retrieval.
- `Categories` (JSON) and `Brand` go into chunk metadata, not the chunk text. Lets us filter retrieval by category if needed.

**Total estimated**: ~6,000 product chunks, deterministically generated
each run. Embedding cost: ~$0.04.

### 2c. Neto Information Pages + brochure PDFs (one unified source via `GetContent`)

**Originally split into two sources** (a brochure-PDF scrape and a separate
blog scrape). Replaced 2026-05-06 after recon on the live `GetContent`
endpoint — it returns every CMS page Neto stores, including:

- Every blog post (`/blog/*`)
- Every product-manual stub page (`/VS135ESmanual` etc.)
- Every exploded-parts-diagram stub page (`/VS135exploded` etc.) ⭐ **new finding**
- Brand pages ("Suits Stihl", "Suits Baumr-Ag")
- Category descriptions ("Water Pumps", "Protective Equipment", "Chainsaw Spare Parts")
- Policy / FAQ / About pages (`/page/*`)
- Reference materials index (`/page/product-manuals/`, `/page/honda-parts-catalogue/`)
- Per-product info pages (`/{slug}infopage`)
- Notices ("Delivery Delays to Some WA and NSW Customers")

The PDF brochures are referenced *inside* `Description1` of the manual
and exploded-view pages. So we don't need a separate scrape to discover
them — one API endpoint walks the CMS, and the same loop extracts both
the page text AND the PDF URLs to download next.

**Reconnaissance findings (2026-05-06, live API run)**:

| Cohort | Count | Notes |
|---|--:|---|
| Active CMS rows total | **1,015** | All page types combined |
| Rows with > 500 chars body content | **156** | The "actually has substance" set |
| Blog posts (`/blog/*`) with body | 56 | Long-form guides |
| Brochure-stub pages (`/{slug}manual`) | 48 | Short page → links to PDF |
| Exploded-view pages (`/{slug}exploded`) | 44 | Short page → links to parts diagram PDF |
| `/page/*` formal pages | 8 | About, manuals index, catalogue, etc. |
| Brand / category pages with substantial copy | ~60 | "Suits Stihl", "Water Pumps", "Layflat Hoses", etc. — surprisingly rich |
| **Unique PDF URLs referenced anywhere** | **93** | 48 brochures + 44 exploded views + 4 alt-path manuals + 2 catalogues + misc |

Compared to what we'd have got from the homepage scrape (33 PDFs + 61 blog posts), the API yields **93 PDFs + 156 substantial pages** — almost 3× the brochure coverage and entirely new layers (exploded views, brand pages, policies).

#### Auth (Neto API)

Credentials already exist in this workspace (`chainsaw-functions/credentials.md`).
Move them into GCP Secret Manager:

```bash
printf "%s" "adil_auto_user"                       | gcloud secrets create neto-api-username --data-file=-
printf "%s" "7rVwFd2PSM0CE6RVSVaej5O7vTpDYIxe"     | gcloud secrets create neto-api-key      --data-file=-
```

(Same key already in use by the `chainsaw-functions/neto-packaging/` Cloud
Functions, so we're reusing infrastructure rather than minting a fresh
key. Permission scope is broad — it currently has at least Read+Update
on Items, and confirmed Read on Content. Watch for credentials.md being
gitignored before pushing anywhere.)

#### `GetContent` request

```
POST https://www.chainsawspares.com.au/do/WS/NetoAPI
NETOAPI_ACTION:   GetContent
NETOAPI_USERNAME: adil_auto_user
NETOAPI_KEY:      <secret>
Content-Type:     application/json
Accept:           application/json
```

Body — paginate 200 rows per page, filter to active, ask for everything
useful:

```json
{
  "Filter": {
    "Page": 1, "Limit": 200, "Active": "True",
    "OutputSelector": [
      "ID","ContentName","ContentURL","ContentType","ParentContentID",
      "DatePosted","DateUpdated","Author",
      "ShortDescription1","ShortDescription2","ShortDescription3",
      "Description1","Description2","Description3",
      "Label1","Label2","Label3",
      "SEOMetaDescription","SEOMetaKeywords","SEOPageHeading"
    ]
  }
}
```

Response shape: `{ "Content": [ … ], "Ack": "Success" }`. Stop paginating
when a page returns < 200 rows. For incremental refresh, add
`"DateUpdatedFrom": "<last-run-iso>"` to the filter.

**Caveat**: every store row I checked had `ContentType` empty. JJ doesn't
classify pages with that field, so we can't filter to "just blog" —
we walk all rows and **classify by `ContentURL` pattern** in our pipeline:
- `blog/*` → editorial / blog
- `*manual` → product manual stub (extract embedded PDF URL)
- `*exploded` → exploded view stub (extract embedded PDF URL)
- `*infopage` → per-product info hub
- `page/*` → formal CMS page
- everything else with > 500 chars body → category / brand / policy

#### What we ingest from each row

For every row with > 500 chars of body, emit one chunk with:

```
# {ContentName}
URL: https://www.chainsawspares.com.au/{ContentURL}
{Author if any} · {DatePosted} (updated {DateUpdated})
Labels: {Label1}, {Label2}, {Label3}

{ShortDescription1 if any (stripped HTML)}

{Description1 (stripped HTML)}
{Description2 if any (stripped HTML)}
{Description3 if any (stripped HTML)}

SEO: {SEOMetaDescription}
```

Cap chunk text at 20 KB (one page — `/page/back-in-stock/` — has a
500 KB body which is almost certainly meta-tag pollution in HTML).
Chunking strategy from §4.4 applies (~500 tokens per chunk, 50-token
overlap).

#### Discovering and ingesting PDFs from the same loop

```python
PDF_RE = re.compile(r"https?://[^\"' ]*\.pdf|/assets/[^\"' ]+\.pdf", re.I)

for row in walk_get_content():
    body_html = " ".join(row.get(f) or "" for f in
                         ("Description1","Description2","Description3"))
    # Emit text chunk for the row itself if it has substance
    if len(strip_html(body_html)) > 500:
        yield row_chunk(row, body_html)

    # Extract any PDF reference and queue for download
    for m in PDF_RE.findall(body_html):
        pdf_url = m if m.startswith("http") else f"https://www.chainsawspares.com.au{m}"
        yield ("pdf", pdf_url, row["ContentURL"], row["ContentName"])
```

PDF download → text extraction reuses the SharePoint pipeline (§4.3):
`pdfplumber` for digital PDFs, Vertex AI Document AI for scanned ones.

| # | Source | Treatment | Why |
|--:|---|---|---|
| 20 | Neto `GetContent` API — substantial CMS rows (~156 pages) | Walk + paginate. Classify by URL pattern. Emit one chunk per row with body text. | Blog posts, brand pages, category descriptions, policies, About, Help, FAQs, info pages — all in one paginated walk |
| 21 | PDFs referenced by those CMS rows (~93 unique URLs) | Dedup by URL. Download with `If-Modified-Since`. Same extraction pipeline as SharePoint product manuals. | 48 brochures + **44 exploded-parts diagrams** (new!) + 1 Honda catalogue + 1 JJ product catalogue + misc — same files the customer is reading |

**Volumes**:
- §20: ~156 chunks at ~5 KB each ≈ 800 KB body text → 200-400 vector chunks. Embedding cost: well under $0.05.
- §21: ~93 PDFs raw size unknown but dominated by a few 100+ MB scanned manuals; after extraction probably 10-20 MB text → 2-5k chunks. Embedding cost: under $0.20.

**Total combined**: ~5,000 chunks, embedding cost under $0.30.

**Why this single source replaces what was two sources**:
- One auth, one paginator, one extraction loop — half the code
- Catches ~3× more PDFs than the homepage scrape (93 vs 33)
- Catches the **exploded-parts diagrams** which are pure agent gold
  (customer says "the bolt holding the bar in place is loose" — agent
  pulls the diagram, points to part #14)
- Catches all the brand and category pages we'd otherwise ignore — these
  contain hand-written explanations of what differentiates products
- Incremental refresh server-side via `DateUpdatedFrom`

**Refresh cadence**: daily. Cheap, and blog/policy edits should land in
the KB fast. Implementation: track `max(DateUpdated)` from last run in
a small KV table, pass that as `DateUpdatedFrom` next time.

### 2d. Sources deliberately excluded from Phase 1

| Source | Reason |
|---|---|
| Staff Site → Online CS → `Picks/` (142 GB) | Warehouse pick slips. Not knowledge. |
| Staff Site → Warehouse → `(Backupify Restore 2022-01-09)` (8.6 GB) | Old backup, not live content. |
| Staff Site → Warehouse → `Container Unloading/`, `Return documentation/` | Operational photos, messy. |
| `/sites/Admin` → Payroll, Human Resources, Accounts | Sensitive. Out of bounds. |
| `/sites/Admin` → Executive (47k files / 65 GB) | 2021 historic dump, mostly photos/videos. |
| `/sites/Admin` → Management (~7k files of 11 GB) | Mostly images, videos, OneNote — low text density. |
| `/sites/JonoJohnoExecutiveTeam`, `/sites/CharlieGrantFahadHaider` | Financial / executive — sensitive. |
| All `*.msg` (Outlook saved emails) | Phase 1 skips. The dedicated email pipeline (see BACKLOG) handles inbox content properly. |
| All `*.mp4`, `*.mov` videos | Phase 1 skips. Phase 2 add: STT transcription via Speech-to-Text v2. |
| All `*.jpg`, `*.png`, `*.heic` (standalone) | Phase 1 skips. Embedded images inside PDFs handled by OCR. |
| `*.one`, `*.onetoc2` (OneNote) | Painful to extract reliably. Defer. |

---

## 3. Architecture

```
                                  ┌─────────────────────────────┐
                                  │   GCP Secret Manager        │
                                  │   sharepoint-tenant-id      │
                                  │   sharepoint-client-id      │
                                  │   sharepoint-client-secret  │
                                  └──────────────┬──────────────┘
                                                 │
                                  ┌──────────────▼──────────────┐
                                  │   kb-ingest (Python)        │
                                  │   • Graph API → download    │
                                  │   • text extract (per type) │
                                  │   • chunk (~500 tokens)     │
                                  │   • embed (Vertex AI)       │
                                  └──────────────┬──────────────┘
                                                 │
                                  ┌──────────────▼──────────────┐
                                  │   BQ kb.kb_chunks           │
                                  │   (vector index)            │
                                  └──────────────┬──────────────┘
                                                 │
       ┌─────────────────────────────────────────┴────────────┐
       │                                                       │
┌──────▼─────────────────────┐                ┌────────────────▼────────────┐
│  chainsaw-ops Flask        │                │  future: agent-copilot      │
│  /api/kb/search            │                │  /api/kb/search             │
│  • embed query             │                │                             │
│  • vector search BQ        │                │                             │
│  • Gemini Flash synthesise │                │                             │
└──────┬─────────────────────┘                └─────────────────────────────┘
       │
       ▼
  Customer 360 card → search box at top
```

**Two execution modes:**
- **One-time backfill** (Phase 1 launch): run `kb-ingest` locally
  against the full source list. Embeds ~3.4k SharePoint files + 6k
  Dataform rows.
- **Refresh** (Phase 1 deferred to Phase 2): same script run from a
  cron / Cloud Function. Compares file `lastModifiedDateTime` against
  stored value; re-embeds only what changed. SharePoint webhooks for
  near-real-time deferred to Phase 2.

---

## 4. Components — implementation notes

### 4.1 `app/services/kb_auth.py` — Microsoft Graph token

```python
import functools, json
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from google.cloud import secretmanager

_PROJECT = "chainsawspares-385722"

def _secret(name):
    c = secretmanager.SecretManagerServiceClient()
    r = c.access_secret_version(name=f"projects/{_PROJECT}/secrets/{name}/versions/latest")
    return r.payload.data.decode()

@functools.lru_cache(maxsize=1)
def graph_token() -> str:
    """Cached app-token. 60-min TTL — restart the worker to refresh,
    or wrap with TTL logic if running long-lived."""
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": _secret("sharepoint-client-id"),
        "client_secret": _secret("sharepoint-client-secret"),
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    r = urlopen(
        f"https://login.microsoftonline.com/{_secret('sharepoint-tenant-id')}/oauth2/v2.0/token",
        data=body, timeout=20)
    return json.loads(r.read())["access_token"]
```

### 4.2 `scripts/kb_ingest.py` — the ingestion script

Top-level loop:

```python
SOURCES = [
    # See section 2a — list of (site_url, drive_name, sub_folder_prefix, glob_filter)
    ("/sites/OnlineCustomerServiceTeam859", "Procedures", "", "*"),
    ("/sites/OnlineCustomerServiceTeam859", "Documents", "Product Documents/", "*"),
    # ... etc
    # Special:
    ("ROOT", "Online CS", "Customer Service Team/Products/", "*"),
]

for src in SOURCES:
    for path, item in walk(src):
        if not _is_text_like(item):
            continue
        if _already_ingested(item):  # checks BQ for matching document_id
            continue
        local = download(item)
        try:
            text = extract(local, item["name"])
        except UnsupportedFormat:
            log_skip(item)
            continue
        chunks = chunk_text(text, source_uri=item["webUrl"], path=path, modified=item["lastModifiedDateTime"])
        embeddings = embed(c.text for c in chunks)
        bq_insert(chunks, embeddings)
```

Plus the Dataform sources:

```python
for row in bq.query("SELECT SKU, Name, Description, Specifications FROM dataform.neto_product_list").result():
    text = format_product(row)
    chunks = chunk_text(text, source_uri=neto_url(row.SKU), source_type="dataform_product")
    embeddings = embed(c.text for c in chunks)
    bq_insert(chunks, embeddings)
```

### 4.3 Text extraction — per file type

| Extension | Library | Notes |
|---|---|---|
| `.docx` | `python-docx` | Walk paragraphs + tables. Strips formatting, keeps headings. |
| `.doc` (legacy) | `libreoffice --headless --convert-to docx` | Convert first, then python-docx. |
| `.pdf` (digital) | `pdfplumber` | Page-by-page text extraction. |
| `.pdf` (scanned) | Vertex AI Document AI | Detect via "no text in first page" heuristic; fall through to Document AI. |
| `.xlsx` | `openpyxl` | One chunk per sheet. Skip sheets > 10k rows (likely data dumps, not knowledge). |
| `.xls` (legacy) | `xlrd==1.2` (last version with xls support) | |
| `.pptx` | `python-pptx` | Title + body + speaker notes per slide. |
| `.txt`, `.md`, `.csv` (small) | direct read | |
| `.html`, `.aspx` | `beautifulsoup4` | Strip nav, keep main content. |

**Failure handling:** any file that errors → log to `kb_ingest_errors` BQ table with `file_path`, `error_class`, `error_message`. Run continues. Manual triage afterward.

### 4.4 Chunking strategy

```python
def chunk_text(text: str, *, source_uri, path, modified, source_type="sharepoint", target_tokens=500, overlap=50) -> list[Chunk]:
    """Split on paragraph boundaries when possible; never break sentences mid-word."""
    # Implementation: use tiktoken or vertexai's tokenizer for accurate count.
    # Walk paragraphs, accumulate until target_tokens, emit, slide back overlap tokens.
    ...
```

**Why ~500 tokens:** Vertex AI `text-embedding-005` accepts up to 2k tokens but quality is best at 200-800. 500 is the sweet spot for retrieval relevance + chunk informativeness.

**Why 50-token overlap:** preserves context across chunk boundaries (an answer spanning two paragraphs isn't lost).

**Per-chunk metadata** (stored alongside the embedding):
- `chunk_id` — UUID
- `document_id` — SHA-256 of `(source_uri || lastModifiedDateTime)`. Re-running on an unchanged file produces the same document_id, allowing dedup.
- `source_uri` — Graph webUrl (clickable link back to the file)
- `source_type` — `sharepoint_doc` / `sharepoint_pdf` / `dataform_product`
- `source_path` — human-readable path: `Customer Service Team/Products/Pumps/Pump Troubleshooting.docx`
- `chunk_index` — position within the file (0-based)
- `chunk_text` — the actual text
- `embedding` — `ARRAY<FLOAT64>` of length matching the model
- `file_modified_at` — TIMESTAMP from Graph
- `file_modified_by` — display name from Graph
- `ingested_at` — TIMESTAMP, set at insert time
- `token_count` — INT64 for cost monitoring

### 4.5 Embeddings

```python
from vertexai.language_models import TextEmbeddingModel

model = TextEmbeddingModel.from_pretrained("text-embedding-005")  # 768-dim

def embed(texts: list[str]) -> list[list[float]]:
    """Batches up to 250 inputs at a time (model max)."""
    out = []
    for i in range(0, len(texts), 250):
        batch = model.get_embeddings(texts[i:i+250])
        out.extend(e.values for e in batch)
    return out
```

If `text-embedding-005` is deprecated by the time we build, swap for the
current Vertex embedding model (Gemini `text-embedding-004` etc.). 768-dim
is the standard at time of writing.

### 4.6 BQ schema

```sql
-- Run once, before first ingestion:
CREATE SCHEMA IF NOT EXISTS `chainsawspares-385722.kb`
OPTIONS(location='australia-southeast1');

CREATE TABLE `chainsawspares-385722.kb.kb_chunks` (
  chunk_id          STRING   NOT NULL,
  document_id       STRING   NOT NULL,
  source_uri        STRING   NOT NULL,
  source_type       STRING   NOT NULL,
  source_path       STRING   NOT NULL,
  chunk_index       INT64    NOT NULL,
  chunk_text        STRING   NOT NULL,
  embedding         ARRAY<FLOAT64>,         -- 768-dim
  file_modified_at  TIMESTAMP,
  file_modified_by  STRING,
  ingested_at       TIMESTAMP NOT NULL,
  token_count       INT64
)
PARTITION BY DATE(ingested_at)
CLUSTER BY source_type, document_id;

-- Vector index (BQ Vector Search):
CREATE VECTOR INDEX kb_chunks_emb_idx
ON `chainsawspares-385722.kb.kb_chunks`(embedding)
OPTIONS(distance_type='COSINE', index_type='IVF',
        ivf_options='{"num_lists": 100}');

-- Error log (best-effort failure capture):
CREATE TABLE `chainsawspares-385722.kb.kb_ingest_errors` (
  attempted_at      TIMESTAMP NOT NULL,
  source_uri        STRING,
  source_path       STRING,
  error_class       STRING,
  error_message     STRING
);
```

### 4.7 Retrieval — `app/blueprints/knowledge_base/`

New Flask blueprint, gated on `support.calls.view` (or a new
`support.kb.query` capability).

```python
@kb_bp.route("/api/kb/search", methods=["GET"])
@require_capability("support.kb.query")
def search():
    q = request.args.get("q", "").strip()
    top_k = min(int(request.args.get("top_k", 5)), 20)
    if not q:
        return jsonify({"error": "missing q"}), 400

    # 1. Embed the query
    qvec = embed([q])[0]

    # 2. Vector search BQ
    sql = """
    SELECT base.source_path, base.source_uri, base.chunk_text,
           base.file_modified_at, base.file_modified_by,
           distance
    FROM VECTOR_SEARCH(
      TABLE `chainsawspares-385722.kb.kb_chunks`,
      'embedding',
      (SELECT @q AS embedding),
      top_k => @top_k,
      distance_type => 'COSINE'
    )
    """
    rows = list(bq.query(sql, job_config=...).result())

    # 3. (Optional) Synthesise with Gemini Flash, with citations
    answer = synthesize(q, rows) if request.args.get("synthesize") == "1" else None

    return jsonify({
        "query": q,
        "answer": answer,
        "citations": [{
            "path": r.source_path, "url": r.source_uri,
            "snippet": r.chunk_text[:300],
            "modified": r.file_modified_at.isoformat() if r.file_modified_at else None,
            "modified_by": r.file_modified_by,
            "distance": r.distance,
        } for r in rows]
    })
```

`synthesize()` is a Gemini Flash call: prompt is "answer the question
using ONLY the snippets below; cite by `[path]`; say 'I don't know' if
the snippets don't contain the answer."

### 4.8 UI — search box on the customer card

Insert above the LIFETIME / ORDERS stat strip:

```html
<form hx-get="{{ url_for('knowledge_base.search') }}"
      hx-target="#kb-results" hx-swap="innerHTML"
      class="mb-4 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
  <label class="text-[11px] uppercase tracking-wide text-slate-500">
    Ask the knowledge base
  </label>
  <div class="mt-1 flex gap-2">
    <input type="text" name="q" placeholder="e.g. dimensions of JM7013-2BBx4"
           class="flex-1 rounded border-slate-300 px-3 py-2 text-sm">
    <button class="rounded bg-brand-600 px-4 py-2 text-sm text-white hover:bg-brand-700">
      Search
    </button>
  </div>
  <div id="kb-results" class="mt-2"></div>
</form>
```

The HTMX endpoint returns a small partial: synthesised answer + 3-5
expandable citation cards (each with the source path, modified date,
and a clickable link back to the SharePoint file).

---

## 5. Cost budget

| Component | One-time | Recurring |
|---|---:|---:|
| **Embedding the corpus** | ~$0.10 | ~$0.02/month for refresh |
| **BQ storage (active + index)** | — | ~$0.50/month |
| **BQ vector queries** | — | ~$0.005 per query (small scan) |
| **Vertex embedding (per query)** | — | ~$0.0001 per query |
| **Gemini Flash synthesis (per query)** | — | ~$0.001 per query |
| **Document AI OCR (if needed)** | up to $5 | rare |
| **Cloud Functions / Cloud Run** | $0 | $0 (Phase 1 runs locally) |

**Realistic monthly run-rate at ~50 queries/day across the team:** under $5/month. Negligible.

**One-time ingestion cost: under $1.** Even with Document AI for any
scanned PDFs we encounter, well under $10.

---

## 6. Smoke tests — what "working" means

Before declaring Phase 1 done, the following queries must return the
expected result:

| Query | Expected top-1 source |
|---|---|
| *"What are the dimensions of JM7013-2BBx4?"* | `dataform.neto_product_list` row for that SKU, OR `Customer Service Team/Products/.../HRU216` doc |
| *"VS135ES vertical shaft engine specs"* | `chainsawspares.com.au/assets/brochures/VS135ES.pdf` (the customer-facing manual) |
| *"Bumper Spike Pro for Stihl chainsaws fitment"* | `dataform.neto_product_list` row for `PJ88024` or similar — Description field lists the compatible Stihl models |
| *"What's the best battery chainsaw for home use in Australia?"* | `chainsawspares.com.au/blog/best-battery-chainsaw-australia` — long-form buying guide |
| *"Why is my chainsaw cutting crooked?"* | `chainsawspares.com.au/blog/chainsaw-cuts-crooked` — troubleshooting walkthrough |
| *"How do I create a new order in Neto?"* | `Procedures/Creating a New Order in Neto.docx` |
| *"What's the policy on RMA returns?"* | `Customer Service Team/Policies and Procedures/Jono and Johno _Returns & RMA Policy` |
| *"Pump troubleshooting steps"* | `Customer Service Team/Products/Pumps/Pump Troubleshooting.docx` (this is **literally the doc Bernie's call needed**) |
| *"Honda HRU216 mower blade compatibility"* | The HRU216 sub-folder OR an instruction manual mentioning HRU216 |
| *"Email template for backorder delay"* | `Customer Service Team/Email Templates/Template Responses.docx` |
| *"Chainsaw chain and bar sizing guide"* | `Training Course/Jono and Johno bar and chain combos guide.docx` |

If 6 of 7 above return a top-3 hit, Phase 1 is shipped.

---

## 7. Update strategy — keeping the KB fresh

A KB that goes stale is worse than no KB. Each source has a different
change rate, so we use different cadences. Cheap because we ingest
deltas only — re-embedding everything weekly would be 100× the cost.

### 7.1 Per-source update cadences

| # | Source | Change rate | Detection | Cadence |
|---|---|---|---|---|
| 1-17 | SharePoint files | Low (a few edits/week) | Graph delta query: `lastModifiedDateTime gt {token}` | **Daily 1am Mel** |
| 18 | Neto product list | Medium (new products, descriptions edited weekly) | Dataform `DateUpdated > last_run` | **Hourly 8am-6pm Mel** |
| 19-22 | Brochure / exploded-view PDFs | Very low (new product launches) | GetContent walk + URL hash check | **Weekly** |
| 23-28 | Website CMS pages | Low (occasional blog post, category edit) | GetContent: `$filter=DateUpdatedFrom gt {token}` | **Hourly** |
| 29 | Email archive | High (continuous business hours) | `/messages?$filter=receivedDateTime gt {marker}` per folder | **Hourly** (already wired up via `email_sync.py`) |
| 30-31 | Call transcripts | High (~150 calls/day) | KB job watches for new rows in `call_classifications` | **Daily 11pm Mel** (after analyser finishes) |

Hourly for things that change during business hours; daily for things
that mostly settle overnight; weekly for things that almost never
change.

### 7.2 Shared infrastructure (one pattern, all sources)

**Watermark table** — each source records where it last got to:

```sql
CREATE TABLE kb.source_watermark (
  source_id            STRING NOT NULL,
  last_synced_at       TIMESTAMP,
  last_token           STRING,        -- delta token / ISO timestamp / page #
  consecutive_failures INT64
);
```

Each ingestion job reads its watermark, fetches changes since, writes
a new watermark on success.

**Per-chunk source tracking** — every chunk in `kb.kb_chunks` carries:
- `source_uri` — clickable link back to original
- `content_hash` — SHA-256 of the source content
- `file_modified_at` — from source system
- `embedding_model_version` — see §7.4

This enables cheap "did this actually change?" decisions:

```python
new_hash = sha256(source_content)
stored_hash = get_chunk_hash_for(source_uri)
if new_hash == stored_hash:
    skip                                # metadata-only update; no re-embed
else:
    delete_chunks_where(source_uri = X)
    ingest_fresh()
    update content_hash
```

**Deletion handling** — for each source, "how do we know when something
is gone?":

| Source | Deletion detection | Action |
|---|---|---|
| SharePoint | Graph delta returns explicit "removed" items | Delete chunks |
| Neto product list | Today's SKU set vs. our last snapshot | Delete chunks for SKUs no longer present |
| CMS pages / brochures | Same, by URL | Delete chunks for missing URLs |
| Email | — | **Don't delete**; even if agent moves to Deleted Items, keep in KB for audit |
| Call transcripts | — | **Don't delete**; classifications are append-only |

### 7.3 Scheduling — where the cron lives

Two options, recommended in this order:

1. **systemd timers on the chainsaw-ops VPS** — simplest. One unit per
   source-type (e.g. `kb-sync-sharepoint.timer`, `kb-sync-cms.timer`),
   alongside the existing `cxone-poller.service`. No new GCP setup,
   logs go to `journalctl`. Start here.
2. **Cloud Run Jobs + Cloud Scheduler** — if any source becomes
   flakey, expensive, or needs better isolation. Same infra
   `chainsaw-functions` already uses. Migrate per-source if/when
   needed.

### 7.4 Three failure modes to plan for

| Mode | Symptom | Mitigation |
|---|---|---|
| **Source goes offline** | API returns 5xx or times out | Increment `consecutive_failures`, retry with exponential backoff, alert (email + Slack) at ≥3 consecutive fails. Watermark stays put — next successful run picks up where we stopped. |
| **Delta token expires** (Graph delta tokens valid 45 days) | API returns `410 Gone` | Fall back to full re-walk; on completion, save fresh delta token. One-shot recovery. |
| **Embedding model changes** (Vertex deprecates current one, or we want to upgrade) | Mixed-model corpus = poor retrieval | Keep `embedding_model_version` per chunk. When model changes, run a one-time re-embed background job that walks chunks oldest-version-first. |

### 7.5 Cost monitoring

Each run logs:
- Chunks added / changed / deleted
- Total tokens embedded (track in BQ — alert if > 10× rolling average,
  flags runaway re-embed)
- Wall-clock duration (alert if a hourly job exceeds 30 min — likely stuck)

Daily summary, posted to a Slack channel or daily ops email:

> *"KB updated: SharePoint +3 / Neto products +7 / CMS unchanged /
> 412 emails / 18 calls. Total tokens embedded: 89,230 (~$0.018)."*

### 7.6 Realistic monthly run-rate at this update cadence

| Bucket | Frequency | Approx. monthly cost |
|---|---|---:|
| Embedding token spend (deltas only) | continuous | ~$2-5 |
| BQ storage (corpus + index) | — | ~$0.50 |
| BQ vector search queries (50/day) | — | ~$1 |
| Compute (systemd timers free; Cloud Run Jobs ~$3) | per-run | ~$0-3 |
| **Total** | | **<$10/month** |

The deltas-only update strategy is what keeps it cheap. A naive
"re-embed everything weekly" would be ~$1k/month at this corpus size.

### 7.7 Day-1 add-ons (extends §11 checklist)

When implementing Phase 1, also:

- [ ] Create `kb.source_watermark` table
- [ ] Add `embedding_model_version` column to `kb.kb_chunks`
- [ ] Per-source ingestion job emits watermark row on success
- [ ] systemd timers + service files in `deploy/systemd/` (or Cloud
      Scheduler config)
- [ ] Daily-summary log → Slack webhook (re-use existing channel
      pattern from chainsaw-call-analyzer if there is one)

---

## 8. Rollout plan

### Day 1 — bootstrap

```bash
# In chainsaw-ops repo:
cd /Users/adil/jonoandjohno/chainsaw-ops
git checkout -b feat/kb-phase1

# Create skeletons:
mkdir -p app/blueprints/knowledge_base scripts
touch app/services/kb_auth.py
touch app/services/kb_extract.py
touch app/services/kb_chunk.py
touch app/services/kb_embed.py
touch scripts/kb_ingest.py

# Python deps to add to requirements.txt:
#   python-docx, pdfplumber, openpyxl, python-pptx,
#   beautifulsoup4, google-cloud-aiplatform, tiktoken

# BQ schema:
bq --project_id=chainsawspares-385722 query --use_legacy_sql=false < docs/kb-schema.sql
# (ship that file as part of the same PR)
```

### Days 2–4 — extraction + chunking working end-to-end

Pick **5 representative docs** from different libraries:
1. A `.docx` from `Procedures/`
2. A digital `.pdf` from `Product Documents/`
3. A scanned `.pdf` from somewhere (forces Document AI path)
4. An `.xlsx` from `Customer Service Team/`
5. A `.pptx` from `The Pitch`

Extract → chunk → manually inspect chunk quality. Iterate on
chunking heuristics until the chunks read as coherent passages.

### Days 5–7 — embedding + BQ insert + retrieval

Run the 5-doc set through the full pipeline. Hand-craft 5 queries.
Verify retrieval quality. Tune chunk size if recall is poor.

### Week 2 — full ingestion

Run `kb_ingest.py` against the entire shortlist. Expect ~5-10% of files
to fail extraction (legacy formats, encrypted PDFs, etc.) — they go in
`kb_ingest_errors`, triage manually after.

### Week 3 — UI + capability gating

Wire up the search box on the customer card. Add `support.kb.query`
capability, grant to existing CS roles. Soft-launch to Adil + one
other agent.

### Week 4 — feedback + iterate

Daily check of recent queries (log them — easy to add now): are the
top results actually useful? If not, the fix is usually
- chunk too small (re-chunk with bigger window)
- chunk too big (re-chunk smaller)
- embedding model isn't great for product-spec language (try
  `text-embedding-005` vs `004`, or domain-tune)

---

## 9. Known issues to plan for

| Issue | Plan |
|---|---|
| Older `.doc` (Office 2003 OLE format) | LibreOffice headless conversion in the pipeline. Adds ~2s/file. |
| Encrypted PDFs | Skip with logged error. Decryption is rare and risky. |
| Scanned PDFs (image-only) | Detect by "no text in first page", route through Document AI OCR. |
| Same brochure exists in multiple sites | Dedup at chunk insert: skip if a chunk with the same `(source_path-without-folder-prefix, sha256(text))` already exists. |
| `.docx` with embedded media (videos, images) | Phase 1 ignores media. Phase 2 considers Document AI for image-text extraction. |
| Per-customer file ACLs | Phase 1 assumes the app's read scope is fine for everyone with `support.kb.query`. If we ingest libraries with stricter ACLs, that's a Phase 2 problem. |
| Token rate limits on Vertex embeddings | Built into `embed()` — batch 250, exponential back-off on 429. |
| Graph rate limits | 10k requests / 10 min / app — ingestion well under this. |
| Library moves / renames | Phase 1: re-ingest is full overwrite. Phase 2: track by document_id stability. |
| 15 of 48 brochure manual-slug pages don't expose the PDF via the simple `/assets/brochures/*.pdf` regex | Their pages embed via iframe with full URL, JS-rendered, or use a `data-*` attribute. Phase 1 extraction picks 33 of 48 — for the remainder, fetch the page with a real browser (headless Chrome via `playwright`) so JS renders, then grab the PDF reference. Or: hand-curate the missing 15 from the inventory once. Maintenance overhead either way is low (it's a one-time discovery). |
| `neto_product_list.Description` length pathologically high on some rows (millions of chars) | Cap to 20KB at extraction time, truncate cleanly. Common causes: copy-pasted ad code, embedded `<script>`, repeated boilerplate. Better long-term: a Dataform cleanup model that strips HTML and dedupes. |

---

## 10. Open questions to resolve before Day 1

- [ ] **BQ region**: are we OK with `australia-southeast1`? It's the
      same region as the rest of `chainsawspares-385722` Dataform
      tables. Vector Search is supported there.
- [ ] **Vertex AI region**: confirm `australia-southeast1` has
      `text-embedding-005` available. Fallback: `us-central1`. (Adds
      latency, fine for batch ingestion.)
- [ ] **Document AI**: is it currently enabled in
      `chainsawspares-385722`? Run `gcloud services list --enabled
      --project=chainsawspares-385722 | grep documentai`. If not,
      enable before scanned PDFs are encountered.
- [ ] **Capability**: name `support.kb.query` for retrieval, and
      `support.kb.admin` for re-running ingestion? Confirm naming with
      existing capability scheme.
- [ ] **Where does ingestion run**: laptop for first backfill is fine.
      Long-term: Cloud Run Job, or a `kb-ingest` systemd timer on the
      chainsaw-ops VPS (similar shape to `cxone-poller`).
- [ ] **Neto API credentials in Secret Manager**: credentials already
      exist in `chainsaw-functions/credentials.md` for the Cloud
      Functions. Move them into `neto-api-username` /
      `neto-api-key` secrets in this project. (Permission scope is
      already broad enough — confirmed Read access to Content via
      live `GetContent` recon on 2026-05-06.)

---

## 11. Day-1 checklist

When ready to start, run through:

1. [ ] `git checkout -b feat/kb-phase1` in chainsaw-ops
2. [ ] Verify Document AI enabled (or enable it):
       `gcloud services enable documentai.googleapis.com --project=chainsawspares-385722`
3. [ ] Apply `docs/kb-schema.sql` to BQ (creates dataset + tables + index)
4. [ ] Add Python deps to `requirements.txt`, `pip install -r requirements.txt`
5. [ ] Implement `app/services/kb_auth.py` (token caching)
6. [ ] Implement `app/services/kb_extract.py` (one extract function per file type)
7. [ ] Test extraction on 5 hand-picked files (see Day 2-4 above)
8. [ ] Implement `app/services/kb_chunk.py` (token-aware splitter)
9. [ ] Implement `app/services/kb_embed.py` (Vertex AI client + batching)
10. [ ] Wire up `scripts/kb_ingest.py` (the full loop)
11. [ ] Run on the 5-doc subset, manually inspect BQ
12. [ ] Implement `app/blueprints/knowledge_base/routes.py` (`/api/kb/search`)
13. [ ] Add HTMX search box to `customer_360/card.html`
14. [ ] Add `support.kb.query` capability + grant to relevant roles
15. [ ] Run full ingestion (target: ~3.4k SP files + 6k Dataform rows)
16. [ ] Smoke-test the 7 queries from section 6
17. [ ] Soft-launch to Adil + one CS agent
18. [ ] Write `BACKLOG.md` entry for Phase 2 (refresh automation)
