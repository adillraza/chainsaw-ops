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

## Email pipeline — sales inbox into the customer card

**Why.** Today the customer card shows orders, RMAs, calls, but nothing
from the sales inbox. Agents handling a call often need "have we
emailed this person recently?" context.

**What.** Gated on a separate email-ingestion project. Once that lands,
add three blocks to the card:
- Sales-inbox thread count + most recent date
- Recent email topics (AI-summarised)
- Email sentiment trend

**Effort.** L. Blocked on email pipeline existing. Once it does, the
card-side wiring is M.

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

## Live transcription of the *current* call

**Why.** The agent's hardest job is "what's this person actually
asking?" Real-time transcription would mean the agent can scan as the
caller speaks, catch SKUs / order numbers / addresses without asking
to repeat.

**What.** RC streaming audio → Vertex AI Speech-to-Text streaming →
side panel on the customer card, scrolling as the call progresses.
Probably works well enough at ~2-3 second latency for skim-reading.

**Effort.** L. RC streaming setup is non-trivial; pricing on streaming
STT needs a check.

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
