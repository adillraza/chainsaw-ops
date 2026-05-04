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

**Total: ~3,400 files, ~3.5 GB after filtering out video/image/binary.**

### 2b. Dataform / BigQuery sources

| # | Source | Treatment | Why |
|--:|---|---|---|
| 18 | `dataform.neto_product_list` | One chunk per row: `{SKU} — {Name}\n{Description}\n{Specs}` | The 6k-product catalogue itself. Search "Honda HRU216 blade" returns the SKU instantly. |
| 19 | `dataform.neto_product_list` (specifications JSON) | One chunk per non-empty spec field | Lengths, weights, compatibilities — already structured |

**Total: ~6,000 product chunks, deterministically generated each run.**

### 2c. Sources deliberately excluded from Phase 1

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
| **Embedding the corpus** | ~$0.05 | ~$0.01/month for refresh |
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
| *"How do I create a new order in Neto?"* | `Procedures/Creating a New Order in Neto.docx` |
| *"What's the policy on RMA returns?"* | `Customer Service Team/Policies and Procedures/Jono and Johno _Returns & RMA Policy` |
| *"Pump troubleshooting steps"* | `Customer Service Team/Products/Pumps/Pump Troubleshooting.docx` (this is **literally the doc Bernie's call needed**) |
| *"Honda HRU216 mower blade compatibility"* | The HRU216 sub-folder OR an instruction manual mentioning HRU216 |
| *"Email template for backorder delay"* | `Customer Service Team/Email Templates/Template Responses.docx` |
| *"Chainsaw chain and bar sizing guide"* | `Training Course/Jono and Johno bar and chain combos guide.docx` |

If 6 of 7 above return a top-3 hit, Phase 1 is shipped.

---

## 7. Rollout plan

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

## 8. Known issues to plan for

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

---

## 9. Open questions to resolve before Day 1

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

---

## 10. Day-1 checklist

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
