# chainsaw-ops Backlog

A running list of features and fixes that are worth doing but aren't urgent.
Newest at the top. Move items into a commit when you start them; archive to
the bottom (or delete) once shipped.

Each entry should include:
- **Why** — what triggered the idea, with concrete examples
- **What** — the proposed change, in one paragraph
- **Effort** — rough size: S (afternoon), M (1-2 days), L (a week+)

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

## (older items here as we accumulate them)
