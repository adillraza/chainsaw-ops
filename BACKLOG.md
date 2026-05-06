# chainsaw-ops Backlog

A running list of features and fixes that are worth doing but aren't urgent.
Newest at the top. Move items into a commit when you start them; archive to
the bottom (or delete) once shipped.

Each entry should include:
- **Why** — what triggered the idea, with concrete examples
- **What** — the proposed change, in one paragraph
- **Effort** — rough size: S (afternoon), M (1-2 days), L (a week+)

---

## Follow-up tracker — turn promises into a workable agent task list

**Why.** Real example seen 2026-04-30, caller `0447006770` (Cole):

- Cole calls about Honda HRU216 mower blades (`JM7013-2BBx4`). The
  website lists "170mm from centre of mounting hole to tip" — but his
  existing blades are 170mm *overall*, only 150mm from centre. He calls
  to verify whether the listing is wrong before buying.
- Michele was alone on the phones, couldn't measure a blade in stock,
  and **promised a callback** after lunch.
- 4 days pass. No callback. **No system anywhere knows this promise was
  made.**
- Today (2026-05-04) Cole calls back twice — still chasing. He's now an
  active call right when this idea was sparked.

This is a missed sale plus a customer experience hit, and it's
*invisible* in the current dashboard. The AI transcript classifier
already saw "Michele promised a callback" — but that intent goes
nowhere, just into the transcript text. Multiply by every "I'll email
you the photos / quote / shipping info" promise across the team and
the unseen backlog is probably big.

**What.** A team-wide follow-up workflow, four phases so it can ship
incrementally:

### Phase 1 — Manual follow-ups (foundation)
- New SQLite table `call_followup`: id, session_id, phone,
  customer_username (nullable), assigned_user_id, status (open /
  in_progress / closed), summary, created_at, due_at, closed_at,
  closed_by_user_id
- Comment thread (reuse `Annotation` pattern or new
  `followup_comment` table)
- Button on the call-details modal: **"Flag for follow-up"** →
  creates row, optional summary, optional assignee
- New Customer Service tab **"Follow-ups"** with filter pills
  (*Mine · All open · Overdue · Closed*), click a row to open the
  same call modal plus comment thread + status controls.
- Capability gating: `support.followups.view` (everyone) +
  `support.followups.manage` (claim/assign/close).

### Phase 2 — AI auto-detection
- Extend the call classifier prompt to extract follow-up intent:
  ```
  follow_up_required: bool
  follow_up_reason: short text
  follow_up_action_promised: short text  # "callback after lunch", "send photos", "check stock and reply"
  follow_up_owner_hint: agent | customer | unspecified
  ```
- One-time **bulk re-classify** of the historic transcripts in
  `recording_fetch_status` / `call_classifications`. Cost is negligible
  (Gemini Flash, ~$10 for 10k calls).
- Auto-create `call_followup` rows from the re-classified backlog —
  `status=open, assigned_user_id=NULL`. Filter pill "Unassigned" lets
  the team claim from the pile in spare time.
- Wire the classifier so new calls get the field on first analysis,
  not just the backfill.

### Phase 3 — Customer 360 integration
- Red banner on the customer card when this phone has any open
  follow-up: *"Open follow-up: Michele promised a callback (4 days ago)"*
  with a one-click jump to the task.
- When the agent picks up a call from a phone with an open follow-up,
  the Live Calls drawer card gets a small badge so they know before
  saying "hello".

### Phase 4 — SLA & manager view
- Optional `due_at` per follow-up; "overdue" colour state
- Manager dashboard: who has the most open / overdue tasks, average
  age, weekly closure rate
- Routing rules: auto-assign new follow-ups to the agent who took the
  original call (when known)

**Effort.** L overall, but each phase is M and shippable on its own.
Phase 1 alone removes the "we have no system" problem. Phase 2 turns
it into a list someone can actually attack without manual flagging.

**Open questions.**
- Do follow-ups for callers with no Neto record (like Cole) live by
  phone alone? Yes — phone is the durable key. If the caller later
  registers, the customer card will surface their history *plus* their
  open follow-up by phone match.
- One follow-up per call, or per customer? Probably per call (links
  cleanly to a transcript), with a customer-level rollup view in the
  follow-ups tab so "5 open across this customer" is visible.
- AI false positives? Flag is just "open + unassigned". Agents can
  bulk-dismiss garbage. False negatives matter more (we miss real
  promises) — phrasing in the prompt should err generous.

---

## Disaster recovery — SQLite backup to GCS

**Why.** Code is on GitHub so a destroyed VPS recovers fast on the
deploy side, but **everything in SQLite is a single point of failure**.
If the VPS at `170.64.179.76` is destroyed (DigitalOcean droplet
deletion, disk corruption, hypervisor failure), we lose:

| Table | Recoverable? | What we'd lose |
|---|---|---|
| `user` + `login_log` | ❌ | Every staff account, password hashes, login history |
| `annotation` | ❌ | Every PO/item-level note staff have written. **Most painful loss** — these are hand-written observations like "supplier shipped wrong batch", "customer rejected, repackage" |
| `item_review` | ❌ | Open warehouse review queue with reasons, statuses, comments |
| `pinned_call` | ❌ | Currently-pinned customer calls + agent notes |
| `cached_purchase_order_*` | ✅ | Rebuildable — kick the "Refresh Data" button (~30s) |
| `call_event` | ⚠️ partial | Re-fills as new webhooks arrive; today's calls lost = a day of slightly-less-rich live drawer |
| `internal_phone_numbers` | ✅ | Migration re-seeds 25 rows on first boot |

So 4 of 9 tables are **irreplaceable** without backup. Annotations alone
are years of staff knowledge — losing them would be a real blow.

**What.** Three phases, each independently shippable:

### Phase A — Hourly cron + GCS upload (MVP, half a day)

```bash
# /opt/chainsaw-ops/scripts/backup_sqlite.sh
#!/bin/bash
set -euo pipefail
DB=/opt/chainsaw-ops/instance/users.db
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
TMP=$(mktemp /tmp/users.db.XXXXXX)
sqlite3 "$DB" ".backup '$TMP'"            # online atomic snapshot
gzip -9 "$TMP"
gsutil cp "${TMP}.gz" "gs://chainsawspares-385722-chainsaw-ops-backups/sqlite/users.db.${TS}.gz"
rm -f "${TMP}.gz"
```

- systemd timer: hourly during business hours (9am-7pm Mel) + a
  midnight snapshot. ~12 backups/day.
- GCS bucket with **object versioning** + lifecycle rule: keep 30
  days, then delete.
- Storage: ~50MB compressed × 12/day × 30 days ≈ 18 GB → ~$0.40/month
  at standard storage rates.

**RPO** (recovery point objective): up to 60 minutes of data loss
worst case. **RTO** (recovery time objective): minutes once a fresh
VPS is provisioned and the latest .db.gz is downloaded into
`instance/`.

### Phase B — LiteStream continuous replication (a day)

Once Phase A is comfortable, layer on LiteStream — a daemon that
streams every WAL frame to GCS in real time. Drops RPO from 60min
to seconds. Restore process becomes `litestream restore` → exact
state at any past point. Phase A snapshots stay as a safety net.

### Phase C — Documented + tested DR runbook (half a day)

`docs/disaster-recovery.md` with the exact restore steps:
1. Provision new Ubuntu VPS (or DigitalOcean droplet from a snapshot if we keep one)
2. Clone the repo, run `bash deploy.sh` to set up systemd + nginx
3. Download latest backup from GCS, place at `instance/users.db`
4. Restart `chainsaw-ops`, `cxone-poller`
5. Re-stash secrets (the `.env` file, GCP credentials JSON)

Plus: **run the drill once a quarter** against a throwaway VPS to
confirm we can actually rebuild from cold storage in under 30 mins.

**Other things in scope worth noting:**

- The `.env` file (Flask secret, RC creds, etc.) is on the VPS only.
  Phase A can also back it up encrypted (e.g. via `gcloud kms encrypt`)
  in the same bucket.
- The systemd unit files (`deploy/systemd/cxone-poller.service`,
  `deploy/systemd/chainsaw-ops.service`) ARE in the repo, so they
  recover from GitHub.
- The GCP service-account JSON at `bigquery-credentials.json` —
  noted in CLAUDE.md gotchas as committed (rotate once + move to
  Secret Manager separately; not strictly DR's job).

**Effort.** Phase A alone is half a day. Phase A+C ships the actual
RPO/RTO promise we want. Phase B is a polish on top.

---

## Customer 360 — server-side cache for the call card

**Why.** Loading `/customer/<phone>` does several BQ queries
synchronously: phone bundle (lookup + history + behaviour), customer
rows for matched usernames, SKU → Neto product ID enrichment, plus
the live-merge of today's `call_event` rows. Card load takes 2-4
seconds today. With the live drawer pinging every 3s and agents
opening/closing customer cards mid-call, that's noticeable latency
right when the agent doesn't want it.

The data is also highly cacheable: `customer_360` and friends update
hourly during business hours (now), and most of what we render is
deterministic per phone for the duration of that hour.

**What.** Two-layer cache in `chainsaw-ops`:

1. **Per-phone payload cache** (the whole `Customer360Service.get_card`
   result) keyed by `(phone, customer_360.refreshed_at)`. TTL = 60
   minutes (matches the new hourly Dataform run). Stored in:
     - SQLite as a single `customer_card_cache` table (text JSON
       payload + the BQ refreshed_at it was built from), OR
     - Redis if we want sub-millisecond serve. SQLite is simpler;
       redis only if the SQLite version turns out slow.

2. **Background refresher** that pre-warms the cache for "hot" phones
   (the top N most-recent callers, plus all currently-pinned calls,
   plus everyone in today's call_event). Runs as a systemd timer or
   APScheduler tick, every 5 minutes.

The live-merge of today's `call_event` (the part that bumps the
"call history" panel with today's calls) is NOT cached — it always
runs at request time off SQLite, so a customer who calls twice in
five minutes still sees the second call appear immediately. The
cache only covers the BQ-derived parts.

**Effort.** M. Two phases:
- Phase A — the cache itself with synchronous fill on miss. Ships in
  ~2 days. Card loads drop to <100ms on warm cache.
- Phase B — the background pre-warmer. Another day. Removes the
  cold-cache spike entirely.

**Open question** — do we ALSO cache the active-call-panel lookup
(currently a SQLite query on every render)? Probably not — it's
already fast and depends on data that changes every 3s.

---

## Customer 360 — Merge duplicate Neto customer records

**Why.** Most customer phones in Neto match 2-8 records — same person
re-registered as guest, slightly different email/spelling, etc. The
multi-match panel exposes this clearly now (clickable list to open
each in Neto cpanel) but **agents can't actually merge them**.
Ongoing effect: every report out of Neto double-counts these
customers, the Customer 360 picks one record arbitrarily (highest
LTV), and accumulated lifetime totals are split across the duplicates.

**The blocker.** Neto API (Maropost) **has no merge endpoint**. All
13 API categories enumerated 2026-05-06 — Customer endpoints are
just Add/Get/Update + customer logs. No DeleteCustomer either. The
cPanel UI has merge but it's UI-only.

**Three strategies, ordered by my preference:**

### Option B (recommended) — Local merge-intent tracking + deep-link to cPanel

Agent ticks the records to merge into the primary in our UI. Two
things happen on submit:
1. A row inserted into a new `customer_merge_intent` SQLite table
   (primary_username, secondary_username, marked_by_user_id,
   marked_at, status, completed_at, reason).
2. New tab opens to Neto cPanel's merge page pre-loaded with those
   records (pending: figure out the cpanel URL pattern; ask Adil
   to paste it from a real merge session).

Customer 360 reads the merge_intent table on every card load and
folds secondary records into the primary view immediately —
combining orders, RMAs, calls, emails — without waiting for Neto
to actually be merged.

Why this is the best of three:
- Agent gets immediate consolidated view in our UI
- Neto cleanup still happens (deep-link does the work in cpanel)
- Audit trail of who marked what for merge in our DB
- If Neto merge is delayed/forgotten, our system still shows the
  right thing
- Builds toward the "fuzzy account linking" item below — same
  schema can later be auto-populated by a fuzzy-match algorithm

### Option A — Deep-link only

Same UI but no local tracking. Agent ticks records, button just
opens cpanel. No audit trail in our system, no immediate
consolidated view.

Lighter to build. Loses the immediate UX win. Skip in favour of B.

### Option C — Pure local soft-merge

Just track in our DB, don't bother with Neto cleanup. Customer 360
shows merged view, Neto stays dirty. Other tools reading Neto
(reports, shipping pickers, etc.) still see duplicates.

Reject — doesn't solve the data-quality problem long-term.

**What.** Phased build of Option B:

1. **Schema + migration** — `customer_merge_intent` table, ~30 min.
2. **Service-layer fold-in** — `Customer360Service.get_card` joins
   the intent table, treats marked secondary records as part of
   the primary's data set. Combine orders / RMAs / call_history /
   email_history. ~1 day, including making sure the lifetime
   numbers don't double-count.
3. **Modal UI** — opens from the existing "Merge (planned)" pill on
   the multi-match panel. Pre-selects the highest-LTV record as
   primary, checkbox grid for which others to merge in. Reason
   text field (optional). On submit: insert intent rows, open
   Neto cpanel deep-link in a new tab. ~1 day.
4. **Status tracking** — small "view past merges" admin page so we
   can see what's pending vs completed. Phase 2.
5. **Auto-detect-completion** — periodic check: if Neto's
   customer_360 view shows the secondary username has 0 orders
   (because merge transferred them), mark the intent as
   `completed`. ~half a day, phase 2.

**Effort.** Phase 1+2+3 = ~2.5 days. The full thing including 4+5
is M (~1 week).

**Open questions.**
- The Neto cPanel merge URL pattern. I need to see it from a real
  merge session in cpanel — please paste the URL bar contents the
  next time you do one.
- What permission gates this? `support.customer_merge` capability
  with same membership as `support.calls.view`, probably.
- Should we ALSO run the merge as a `chainsaw-functions` Cloud
  Function (using the existing Neto API key) to call cPanel
  programmatically via headless browser / HTTP scraping? Out of
  scope for phase 1; revisit if "deep-link to cpanel" turns out to
  be a worse UX than expected.

---

## Customer 360 — fuzzy account linking across duplicate Neto accounts

**Why.** Customers often re-register under a new email/username over the
years, leaving their history split across two Neto records. Real example
seen 2026-05-04 for `0407446130`:

| Field | `berniefinlay243` (current) | `BernieFinlay` (legacy) |
|---|---|---|
| Name | "Bernard Finlay" | "Bernie Finlay" |
| Email | `berniefinlay@bigpond.com` | `berniefinlay@bigpond.com.au` |
| Address | 23 elliot st, 2422 | 23 Elliot St, 2422 |
| Phone | 0407446130 (mobile) | 0265581480 (landline) |
| Orders | 19 | (older) |
| RMAs | 0 | **1 historic RMA from 2019** |

The customer card resolves phone → username via `customer_phone_lookup`,
which keys off the phone field of each Neto record. Because the legacy
account has only the landline, the mobile-driven lookup misses it
entirely — agents see "0 lifetime RMAs" when there actually is history.

**What.** Add a `customer_alias_link` Dataform model that fuzzy-matches
Neto usernames to the same physical person using:
- Same surname AND same postcode AND same street (normalised: lowercased,
  whitespace-collapsed)
- OR near-identical email (same local-part, ignore `.au`/`.com.au`)

Then update `customer_360` (or join in `Customer360Service.get_card`) so
that when one phone resolves to username A, we also load A's aliases.

**Effort.** M. Mostly Dataform SQL + a small service-layer change. UI
already handles multi-record customers (the "+1 other matching record(s)"
header) so the template work is minimal.

---

## "Attach to customer" widget — the unknown-caller flywheel

**Why.** ~55% of inbound calls are unmatched (the caller's phone isn't
on file in Neto). The customer card falls into "Unknown caller" mode for
these. Today there's no way for an agent to manually link the call to
the right customer once they've identified them mid-call. Every such
call is a missed opportunity to permanently improve match coverage.

**What.** Inline search box on the customer card, only visible in
unknown-caller mode. Mid-call, the agent types name / email / order ID,
picks the right customer, clicks "Link". Two effects:
- Card flips into full Phase-1 view from that moment.
- Mapping persists in a `phone_to_customer_override` table that the
  daily `customer_360` model reads. Next time this number calls, the
  system already knows.

The self-improving loop expected to claw back ~10–15 percentage points
over the first few weeks of use without any Neto data cleanup.

**Effort.** M. Search endpoint already exists (`customer_360.search`).
Need a small UI affordance + the override table + a `customer_360`
model tweak.

---

## Phone-coverage improvements (lift the 45% match rate)

**Why.** Phone-only matching currently catches 45% of inbound callers.
Format consistency is fine (RC always +61 E.164, Neto always 04… local —
single normalisation), so the gap is data coverage, not parsing.

**What.** Three independent sources to layer in:
1. **Order-level phones** — `neto_orders.BillPhone` / `ShipPhone`
   sometimes carry numbers that aren't on the customer record.
2. **Past call self-identification** — when a caller said their order
   number on a transcribed call, link that phone to that order's
   customer.
3. **Agent-linked overrides** — see the "Attach to customer" flywheel
   item above.

Combined ceiling estimate: 65–70% match.

**Effort.** M for #1 (Dataform), L for #2 (transcript-driven extraction,
shares pieces with #1's resolver model), already-counted for #3.

---

## Multi-agent display — show on the agent who picked up

**Why.** Right now the live drawer shows every in-flight call to every
logged-in user. In a busy contact-centre an agent only cares about
their own call. CXone publishes `agentId` per contact (already in
`call_event.body_json`) so we can route the card.

**What.** Map CXone agent IDs to chainsaw-ops users (probably via a
new column on `User`). When a call's `agentId` matches the logged-in
user, give that card a special "yours" badge or auto-open the customer
360 page on connect.

**Effort.** S–M. Mostly mapping table + a small filter in the drawer.

---

## Sales-inbox pipeline — customer panel + knowledge base feed

**Why.** Two distinct uses for the same data:

1. **Customer 360 panel** — agent on a call wants to know "have we
   emailed this person recently, what about?" Today: zero visibility.
2. **Knowledge base feed** — years of sales emails are the highest-
   quality product knowledge we have. *"Does JM7013-2BBx4 fit HRU216?
   Yes, 21" deck only, won't fit HRU214"* is an answer a real agent
   gave to a real customer in a real thread. Multiplied by every
   compatibility / fitment / spec question we've ever answered:
   institutional memory becomes searchable.

Build the pipe once, light up the panel and the KB feed independently.

### Auth — already half-done

The Azure AD app we registered for SharePoint
(`30ee98d1-7ccc-4315-a1f4-01ce96229962`) is the same Microsoft Graph
client we'd use for mail. Just **add `Mail.Read` application
permission** to the existing app, admin-consent it once, and we're
authenticated against every mailbox in the tenant. Same secret in
GCP Secret Manager (`sharepoint-client-secret`) — could rename to
`graph-client-secret` if we want it to feel less SharePoint-specific.

### Discovery / inventory pass (Day 1)

Before backfilling, list every mailbox in the org:

```python
GET https://graph.microsoft.com/v1.0/users?$select=id,mail,userPrincipalName,displayName
```

Bookmark the shared mailboxes specifically (sales@, info@, support@,
orders@, etc.) — we'll start with these. Personal staff inboxes can be
phase 2 once we know shared works.

### Backfill volume

Estimating: ~150 messages/day inbound + outbound combined across
shared mailboxes. Five years of history = ~270k messages. Each ~5 KB
text body → ~1.4 GB raw. Trivial for BigQuery.

Cost: Graph rate-limit is ~10k requests / 10 min / app, so backfill at
100 messages per page = 2,700 pages = ~30 mins of polling. One-time job.

### Pipeline shape

| Stage | Tool | Output |
|---|---|---|
| Backfill | Python script, paginated `GET /users/{mb}/messages` | `email_archive.messages` BQ table |
| Incremental | `/messages/delta?$filter=receivedDateTime ge ...` keyed on lastDeltaToken | Same table, upserted |
| Webhook (optional) | Graph subscriptions for "new mail" events | Near-real-time row insert |

BQ schema:

```sql
CREATE TABLE email_archive.messages (
  message_id        STRING NOT NULL,           -- Graph immutable ID
  conversation_id   STRING,                    -- thread grouping
  mailbox           STRING,                    -- e.g. 'sales@chainsawspares.com.au'
  direction         STRING,                    -- 'inbound' / 'outbound'
  subject           STRING,
  from_address      STRING,                    -- normalised lowercase
  from_name         STRING,
  to_addresses      ARRAY<STRING>,
  cc_addresses      ARRAY<STRING>,
  received_at       TIMESTAMP,
  sent_at           TIMESTAMP,
  body_preview      STRING,                    -- first ~250 chars for UI
  body_text         STRING,                    -- full plaintext for KB
  body_html         STRING,                    -- preserved for modal render
  has_attachments   BOOL,
  attachment_count  INT64,
  web_link          STRING,                    -- ⭐ Graph-provided OWA URL
  ingested_at       TIMESTAMP NOT NULL
)
PARTITION BY DATE(received_at)
CLUSTER BY conversation_id, from_address;
```

### Use 1 — Customer 360 panel

New "Email history" section on the card, parallel structure to Call
History. For a customer with `email = scott.bremner31@hotmail.com`:

```sql
SELECT subject, direction, received_at, web_link, body_preview, conversation_id
FROM email_archive.messages
WHERE LOWER(from_address) = @email
   OR @email IN UNNEST(to_addresses)
ORDER BY received_at DESC LIMIT 50
```

Display: thread list grouped by `conversation_id`, latest reply first,
each entry shows subject + date + direction icon + the agent (if outbound).

**Click → opens the email in Outlook Web in a new tab.** Microsoft Graph
returns a `webLink` field on every message that does this for free —
no auth needed on the click, OWA handles SSO. Looks like:

```
https://outlook.office.com/owa/?ItemID=AAMkAGI2...&exvsurl=1&viewmodel=ReadMessageItem
```

For native Outlook desktop users, the same URL opens via the Outlook
protocol handler if they have it installed, otherwise falls back to OWA.
Either way: one click, agent reading the actual email.

### Use 2 — Knowledge base feed (links to *Product knowledge base*)

Periodic Dataform job filters threads where the agent reply contains
a product SKU / dimension / fitment claim (regex first, LLM classifier
later). Extract Q&A as a KB chunk:

```
Q: Does JM7013-2BBx4 fit HRU216?
A: Yes, 21" deck only, won't fit HRU214.
— thread 4419, 2024-08-12, agent Dallas
```

Embed + add to vector store with `source = "sales_email"`. Joins the
SharePoint procedures, Neto product descriptions, and brochure PDFs in
the same RAG retrieval pool.

### Phasing — concrete timeline

1. **Phase 1 (1 week): pipeline + read-only customer panel.**
   - Day 1-2: add `Mail.Read` permission, mailbox inventory, schema
   - Day 3-4: backfill script for one shared mailbox (sales@)
   - Day 5: incremental delta-query refresh, hourly cron
   - Day 6: customer-360 query + panel UI + click-to-Outlook
2. **Phase 2 (M): expand to all shared mailboxes + delivery-status emails**
   - Add support@, info@, orders@. Filter out auto-emails (delivery
     receipts, postmaster, etc.) at ingestion time.
3. **Phase 3 (M): thread summarisation**
   - Gemini Flash nightly: one-line summary per conversation_id.
     Display "*3 threads: replacement blade availability, shipping
     delay, after-sale fitment*" at panel header.
4. **Phase 4 (L): KB extraction**
   - Classifier picks product-relevant threads. Embed Q&A as chunks.
     Lights up agent copilot with prior-answer matches.
5. **Stretch: auto-draft replies** — given a new inbound, pre-draft a
   response using customer's order history + KB. One-click send after
   agent review.

**Effort.** L overall, but **Phase 1 is M and ships in 1 week**.
Phase 1 alone gives agents email visibility on the customer card,
which addresses the original ask.

### Open questions

- **Which mailboxes first?** sales@ likely. Confirm with the team
  which shared mailbox sees the most customer correspondence.
- **Body retention** — keep full body forever, or summarise + drop
  after 2 years? Bodies are ~1 GB/year; retention is cheap. Default
  to keeping forever unless there's a privacy reason.
- **Personal staff inboxes** — out of phase 1. Each staff member's
  individual inbox would be useful but raises consent + scope
  questions. Defer.
- **Attachments** — defer phase 1 to text only. Phase 4 OCRs PDFs +
  images via Document AI when we add KB extraction.
- **PII / privacy** — emails contain personal info beyond what the
  customer card normally shows. Cap visibility behind the
  `support.calls.view` capability (already in place). No new gating
  needed for phase 1.

---

## Cross-channel timeline (calls + orders + emails + RMAs merged)

**Why.** The card has separate panels for orders, calls, RMAs. Agents
mentally stitch the timeline. A unified "what happened with this
customer in chronological order" view would be the cleanest single
read.

**What.** A timeline component, pulling from all four sources, sorted
by datetime, with a small icon per event type. Probably collapsible:
"Show all 47 events" vs the most recent 10.

**Effort.** M.

---

## Product knowledge base — RAG over 6k products + manuals

**Why.** We carry 6,000+ active products with hundreds of brochures
and manuals (PDFs). No agent can carry that in their head. Cole's call
(see *Follow-up tracker* above) is the canonical case: "what's the
overall length of this blade?" is a 5-second answer if you can search
the spec sheet, a 4-day callback if you can't.

A queryable knowledge base over the catalogue + product docs is the
single biggest agent productivity unlock we could build, and it's
**cheap and tractable today**.

**What.** A standard RAG pipeline:
- **Ingest**: `dataform.neto_product_list` attributes + REX product
  data (already in BQ), plus PDF manuals/brochures (Vertex AI Document
  AI for OCR + extraction).
- **Chunk + embed**: ~500-token chunks, Vertex AI `text-embedding-004`.
  One-time cost for 6k products + manuals ≈ **$2**.
- **Store**: BigQuery Vector Search — we already pay for BQ, this adds
  effectively zero infra. (Vertex AI Vector Search is the alternative
  if BQ Vector turns out to have limits.)
- **Query path**: agent types → embed → top-5 chunks → Gemini Flash
  answers with citations to the source document. ~$0.001 per query.
- **UI**: search box at the top of the Customer 360 card (or a
  dedicated "Ask the catalogue" tab in Customer Service). Returns
  answer + chunks + clickable links to the source PDFs in GCS.

**Phasing:**
1. **Phase 1 — full implementation spec ready.** See
   [`docs/kb-phase1-spec.md`](docs/kb-phase1-spec.md). Covers:
   exact source list (~3.4k SharePoint files + 6k Dataform rows),
   architecture, BQ schema with vector index, extraction strategy
   per file type, chunking strategy, retrieval API, UI sketch, cost
   budget (under $1 one-time + ~$5/month run rate), 7 smoke-test
   queries, week-by-week rollout plan, day-1 checklist. Built on
   Microsoft Graph credentials already in GCP Secret Manager and a
   thorough SharePoint reconnaissance (`docs/sharepoint-*.md`).
   **Ships in ~3 weeks of focused work.**
2. **Phase 2 — refresh automation.** SharePoint webhooks → Cloud
   Function → BQ. Re-embed only changed files.
3. **Phase 3 — per-customer panel on the 360 card.** "About this
   customer's products" — auto-loads spec snippets for the SKUs
   they've bought. Zero typing required for the most common case.

**Effort.** L overall, but Phase 1 is M. Phase 1 alone gives agents
a useful catalogue search even without manuals.

**Open questions.**
- Where do the PDFs currently live? Need a manifest + GCS upload
  pipeline.
- Spec sheets that exist as **images** (not text PDFs) — Document AI
  handles those but quality varies.
- Versioning when product specs change (mowers replaced, suppliers
  switched). Probably re-embed monthly.

---

## Live transcription of the *current* call

**Why.** The agent's hardest job is "what's this person actually
asking?" Real-time transcription would mean the agent can scan as the
caller speaks, catch SKUs / order numbers / addresses without asking
to repeat. Plus it's the foundation for an agent copilot (see
*Agent copilot* below).

**What.** Two parts, with very different effort:
- **Transcription itself** is the easy bit. Google Cloud Speech-to-Text
  v2 streaming, AU English, ~300ms latency, ~$0.024/min — essentially
  free at our call volume.
- **Getting the audio stream** is the hard bit. Three paths, ranked by
  realism:
  1. **CXone Real-Time Audio (RTA)** — NICE/CXone exposes a real-time
     audio WebSocket for "agent assist" integrations. **Worth a 2-hour
     spike to confirm whether our tier includes it.** If yes, this is
     the clean path: pipe the WebSocket → Google STT → side panel.
     ~1 week of integration work after the spike.
  2. **RingCentral Media Streaming** — equivalent for the store/PBX
     calls. Same shape, different vendor.
  3. Custom RTP/SIP pipe — months of telephony work; only if both
     above paths are blocked.

**Phasing:**
1. **Spike**: confirm what audio API our CXone tier exposes
   (and what RC offers for the PBX side).
2. Live transcript side panel on the customer card — agents can
   scan as caller speaks. Standalone value even without copilot.

**Effort.** Spike: S (afternoon). Phase 1 build: L if API access works
out, much larger if we have to build telephony infra ourselves.

---

## Agent copilot — live transcript × knowledge base

**Why.** Once both *Product knowledge base* and *Live transcription*
exist, the natural composition is: detect SKUs / product names /
intent words in the transcript stream → fire a KB query → drop the
answer onto the agent's screen in real-time.

**Cole's call would have looked like this:**
- Cole says "JM7013-2BBx4"
- System detects the SKU, queries KB
- Sidebar shows: *"170mm overall, 150mm centre-to-tip, mounting hole
  13mm — source: Honda HRU216 service manual p.4"*
- Agent reads it back to Cole, closes the sale on the call

This is a real product category — Cresta, Observe.AI, Salesforce
Einstein for Service all do it. We can build the equivalent for our
catalogue at a fraction of the cost.

**What.**
- Keyword detector running on the transcript stream — SKUs, brand
  names, dimension queries, order IDs, common problem patterns
- KB query fired on every detected hit, results de-duplicated against
  what's already on screen
- Suggestion cards in a transcript-side rail, click to expand the
  source document
- (Stretch) Gemini summarises mid-call: *"Customer is a homeowner
  asking about replacement blades for their HRU216. They've
  measured their existing blades. They want pre-purchase
  confirmation that ours fit."*

**Effort.** L. Depends entirely on phases 1 and 2 of the KB and Live
Transcription items.

---

## Suggested next-best-action

**Why.** With the full customer context (orders, RMAs, sentiment,
problems detected, transcripts) we have enough to suggest moves the
agent might miss in the heat of a call. e.g. *"Offer 10% off — they've
had 2 RMAs in 6mo and are talking about cancelling."*

**What.** LLM call (Gemini Flash) over the customer 360 payload at
card-load time. Output a short bullet list of suggestions with reasons.
Cached aggressively (only re-run when their data changes).

**Effort.** M. Prompt design is the hard part; infra is light.

---

## Auto-draft follow-up email

**Why.** Agents often promise "I'll email you the photos / shipping
info / quote" at end of call. Drafting the email is a context-switch
they often delay or forget.

**What.** One-click button on the call-details modal: "Draft email
based on this transcript". LLM produces a draft, opens in a compose
view (or copies to clipboard, or POSTs to Gmail API).

**Effort.** M. Depends on email pipeline (similar API surface).

---

## (older items here as we accumulate them)
