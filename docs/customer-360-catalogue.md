# Customer 360 — Live Call Context Catalogue

Living spec for the real-time customer-context feature attached to inbound calls.
Origin: planning session 2026-05-03. Update freely as scope solidifies.

---

## Purpose

When an agent picks up an inbound call, the Ops Dashboard surfaces every useful
piece of context we already have about the caller. Goal: agent has the customer's
story in front of them before they say "hello".

The card is driven by **CallerID phone number** as the primary identifier
(empirical match rate 45–55% on raw RC inbound, see "Match-rate baselines"
below), with an "unknown caller" mode for the rest.

---

## Catalogue (organised by build phase)

### Phase 1 — Profile + transactional foundation

The minimum that makes the card useful. Pure SQL aggregations from data we
already have. No AI yet.

| Block | Source | Notes |
|---|---|---|
| Header: name, email, address, phone | `dataform.neto_customers` | Card identity strip |
| Customer since | `MIN(DatePlaced)` over `dataform.neto_orders` | First-order date, not signup |
| Lifetime value | `SUM(GrandTotal)` over `neto_orders` | One number, big font |
| Order count + AOV | `COUNT` and derived | Quick health read |
| Most recent order — full lines | `neto_orders` + nested `OrderLine` JSON | All items, qty, prices, tracking, status |
| Top-N items ever bought | `neto_orderline` aggregated | "Mostly buys chains and bars" |
| RMAs / warranty claims | `dataform.neto_rmas` | Count + most recent + return rate |
| Last RMA — returned line items | `dataform.neto_rma_lines` | The actual SKUs + return reason + outcome |
| **Retail Express in-store purchases** | `ballaratv2.Customers/Orders/Order_Items`, joined to Neto by email/phone | In-store activity for omnichannel customers |

### Phase 2 — Call history context

Cheap to add once Phase 1 is live; uses the same `customer_360` daily model.

| Block | Source | Notes |
|---|---|---|
| Total calls (lifetime) | `ringcentral.account_call_log_leg` + `ringcentral_jnj.*` | Joined on normalised phone |
| Breakdown by disposition | same | Connected / Missed / Abandoned / Voicemail |
| Last 5 calls (compact table) | same | Date · duration · agent · disposition |
| Average call duration | same | Customer "style" indicator |
| Days since last call | same | Hot vs cold |

### Phase 3 — AI behaviour insights (no Vertex AI needed!)

Originally planned as a daily Vertex AI batch — turned out the data is already there.
chainsaw-call-analyzer writes structured AI output to `ringcentral_jnj.recording_fetch_status`
(transcript, summary, sentiment JSON, topics, intents) and `ringcentral_jnj.call_classifications`
(call_type, sale_result, problems_detected, escalation_actions, etc.).
Phase 3 is just an aggregation join.

| Block | Source | Notes |
|---|---|---|
| AI summary (last call) | `recording_fetch_status.summary` | Free text per call |
| Sentiment label + score | `recording_fetch_status.sentiment` JSON `.average.*` | Per-call + average across all analysed |
| Top reasons for calling | UNNEST `call_classifications.delivery_tracking.reason_for_call` | top 3 per phone |
| Top problems detected | UNNEST `call_classifications.problems_detected` | top 3 |
| Top call types | UNNEST `call_classifications.call_type` | top 3 |
| Sales outcome distribution | `call_classifications.sale_result` | counts |
| Last call full bundle | most recent classified call | summary + transcript + structured fields |

**⚠️ Do NOT use `dataform.rc_analysis_transcripts`** — it has multi-select unnest duplicates.
Use the source-of-truth tables in `ringcentral_jnj`.

### Phase 4 — Computed badges

Rule layer on top of phases 1–3. Read by the UI as a single column on the
`customer_360` row.

| Block | Source | Notes |
|---|---|---|
| Customer badge | rule-based | `gold` / `regular` / `watchlist` / `new` / `lapsed` |
| Risk flags | RMA rate, refund history, abandoned-call count | Watchlist signal cluster |

### Phase 5+ — Email and stretch features

Moved to [`BACKLOG.md`](../BACKLOG.md). Includes the email-pipeline
panels (sales-inbox count, AI-summarised topics, sentiment trend) and
the "magic" stretch features (live transcription, next-best-action,
auto-draft email, cross-channel timeline).

---

## Unknown-caller mode (~55% of inbound calls)

Phone-only matching misses about half of inbound calls. The card still has to be
useful in that case. Three things we always show even without a customer match:

**1. Number metadata** — formatted nicely, region inferred from prefix
("Vic mobile", "Ballarat landline"), and "first sighting" vs "Nth call from this
number".

**2. Past calls from the same number** — RC indexes calls by `from_phone_number`
regardless of customer linkage. So even unknown numbers carry history:

> *5 prior calls from this number. Most recent: 3 days ago, spoke to Sam for
> 6 min, ended cleanly. Before that: 2 missed calls last week.*

**3. Past transcripts from the same number** — same idea, applied to the
call-analyzer pipeline. If we've transcribed prior calls from this number we
can summarise them by phone, not by customer.

**4. Inline "Attach to customer" widget** (flywheel) —
see [`BACKLOG.md`](../BACKLOG.md). Mid-call agent linking turns every
unknown-caller into a permanent mapping. Expected to claw back ~10–15
percentage points of match rate without any Neto data cleanup.

---

## Architecture sketch

```
RingCentral ── live call events ─→ ops-call-listener (Flask blueprint)
                                          │
                                          ▼
                                   active_calls table (SQLite, on the VPS)
                                          │
                                          ▼
       ┌────────── Server-Sent Events / HTMX SSE ──────────┐
       ▼                                                   ▼
  Sidebar                                       Agent's main pane
  (live in-flight calls)                       (rich customer card on click)
                                                       │
                                                       ▼
                                       ┌──── Customer 360 view ────┐
                                       │  Phase 1 blocks            │ ← daily Dataform `customer_360` (cached)
                                       │  Phase 2 blocks            │ ← same
                                       │  Phase 3 blocks            │ ← same (AI columns)
                                       │  Most recent order         │ ← live BQ (cached 30–60s)
                                       └────────────────────────────┘
```

**Two refresh cadences:**
- **Live (seconds)**: active calls list. Driven by RC webhooks → backend → SSE
  to browser. Falls back to polling.
- **Daily (batch)**: customer 360 attributes built by the Dataform model + a
  Vertex AI batch job for the AI columns. Agent-click latency is then a fast
  key-lookup, not an LLM call.

The "most recent order" specifically stays on a live cache (not the daily snapshot)
so a customer who placed an order an hour ago is reflected immediately.

---

## Match-rate baselines (2026-05-03 sample)

Distinct inbound numbers in the last 14 days, after filtering JJ's own
store/transfer line:

| Cohort | Distinct callers | Call legs |
|---|---|---|
| Matched 1:1 | 130 (39%) | — |
| Matched 2–5 customers | 19 (6%) | — |
| **At least one match** | **149 (45%)** | **51% weighted** |
| Unmatched | 184 (55%) | 49% weighted |

Format consistency is excellent (RC always +61 E.164, Neto always 04…
local — single normalisation), so the unmatched cohort is a data-coverage
issue (number not on file), not a normalisation issue.

See [`BACKLOG.md`](../BACKLOG.md) for the three layered ideas to lift
this ceiling to ~65–70%.

---

## Open questions

- **PII / capability gating** — ✅ implemented as `support.calls.view`.
- **RingCentral live-event API access** — ✅ resolved: we use RC PBX
  webhook subscriptions (push) plus a long-polling `cxone-poller` daemon
  for CXone (`/contacts/active`).
- **Multi-agent display** — open. See [`BACKLOG.md`](../BACKLOG.md):
  *Multi-agent display — show on the agent who picked up*.
- **Cross-channel ID resolution** — open; a partial answer is already
  in [`BACKLOG.md`](../BACKLOG.md) under
  *Customer 360 — fuzzy account linking* and *Phone-coverage improvements*.

---

## Status

| Phase | Status |
|---|---|
| Catalogue drafted | ✅ 2026-05-03 |
| Match-rate baseline | ✅ 2026-05-03 |
| Schema design + filters locked in | ✅ 2026-05-03 |
| Phase 1 models live: `customer_360` (327k), `customer_phone_lookup` (312k), `customer_rex_link` (5k) | ✅ 2026-05-03 |
| Nightly schedule wired (23:00 Mel daily) | ✅ 2026-05-03 |
| Phase 2 model live: `call_history_360` (34k phones) | ✅ 2026-05-03 |
| Phase 3 model live: `call_behavior_360` (2k phones with AI insights) | ✅ 2026-05-03 |
| Flask blueprint + UI | ✅ 2026-05-03 |
| Live call detection (RC PBX webhook + CXone poller → drawer) | ✅ 2026-05-04 |
| Pinned calls (team-shared) + 3-section drawer | ✅ 2026-05-04 |
| Master-contact + cross-platform overlap merge | ✅ 2026-05-04 |
| Email integration | gated on email project — see `BACKLOG.md` |
| Stretch features (live transcription, next-best-action, auto-draft) | see `BACKLOG.md` |

---

## Locked-in design decisions (2026-05-03)

1. **Customer key = Neto `Username`** even though it's auto-generated. `neto_orders` has no `CustomerID` field, so `Username` is the only join key. Same phone may map to multiple Usernames (legitimate guest-checkout pattern); the lookup returns an array and the UI handles disambiguation.
2. **Order date pivot = `DatePlaced`** (DATETIME, already parsed). Used for `customer_since`, `last_order_date`, recent-order ordering.
3. **Order filter** — **completed and paid** only:
   `OrderStatus = 'Dispatched' AND CompleteStatus = 'Approved'`
   (~98% of all orders; covers the meaningful business universe.)
4. **Internal store-account exclusion**:
   `Username NOT IN ('Showroom', 'warrackjono1960', 'JJWarranty')` (Ballarat store, Warrack store, warranty staff)
   `AND SalesPerson != 'Haiderali'` (staff member who creates store-transfer orders under various customer accounts)
   Net retention: **97.9% of all orders**, **309k real customers**.
5. **Phone normalisation**:
   - AU `+61` → `0` (e.g. `+61419565200` → `0419565200`)
   - Existing AU local left alone
   - International (`+...` non-AU, short codes, malformed) kept as-is and tagged `is_international = TRUE`
6. **Refresh schedule = 23:00 Mel daily**. Outside the half-hourly Purchase Orders window so workflows don't fight for slots.
7. **REX → Neto identity link** is best-effort fuzzy match on lowercased email primary, normalised phone secondary (a separate `customer_rex_link` model). Many customers won't match (online-only or in-store-only); that's expected.
