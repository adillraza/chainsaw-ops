"""Customer 360 BigQuery service.

Loads the data backing the live-call customer card from the four
``dataform.customer_*`` and ``dataform.call_*`` tables built by
``misc_ai_work/chainsawspares-dataform/definitions/Customer Service/``.

Two BQ queries per card load:

  1. The "phone-keyed" bundle: phone_lookup + call_history_360 +
     call_behavior_360 in a single SELECT (all share the phone PK).
  2. ``customer_360`` rows for the usernames the lookup returned.
     Skipped entirely when there's no match (unknown-caller mode).

The service reuses the global BigQuery client owned by
:mod:`app.services.purchase_orders_service` so we don't hold two
connection pools open.
"""
from __future__ import annotations

import functools
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

from app.extensions import db
from app.models.customer_cache import (
    CachedCallBehavior,
    CachedCallHistory,
    CachedCustomer360,
    CachedEmailMessage,
    CachedEmailRecipient,
    CachedNetoProduct,
    CachedPhoneLookup,
)
from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"
DATASET = "dataform"

# Make scripts/ importable so we can reuse Graph + ingest helpers from the
# email_backfill script without duplicating ~80 lines of auth/parsing here.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

log = logging.getLogger(__name__)

# Module-level caches so repeated Customer 360 card loads don't pay
# the Graph auth + folder-list cost on every request.
_GRAPH_TOKEN: dict = {"value": None, "expires_at": 0.0}
_FOLDER_MAP: dict = {"value": None, "expires_at": 0.0}

# Per-cache-table readiness flags, memoised for 60s. Each table is
# decided independently because some tables (e.g. customer_360) may
# legitimately be empty while others are loaded.
_CACHE_READY_FLAGS: dict[str, dict] = {}

# Per-phone pre-warm dedup. The CXone/RC webhook fires many events per
# call (ring, answer, transfer, hangup) — we only want to pre-warm once
# per phone per minute. After 60s, if a call is still live we re-warm
# so the email panel is fresh right before the agent opens the card.
_PREWARM_RECENT: dict[str, float] = {}
_PREWARM_TTL = 60.0


def _lifetime_value_key(customer: dict) -> float:
    """Sort key for ranking customers by lifetime value.

    BigQuery NUMERIC fields round-trip through json.dumps(default=str) as
    strings (e.g. ``"3500.50"``). Python can't compare ``int`` to ``str``,
    so this coercion stops the sort from blowing up on mixed types.
    """
    v = customer.get("lifetime_value")
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _cache_table_ready(model, key_col) -> bool:
    state = _CACHE_READY_FLAGS.setdefault(model.__tablename__,
                                          {"value": False, "checked_at": 0.0})
    now = time.time()
    if state["value"] and now - state["checked_at"] < 60:
        return True
    try:
        state["value"] = db.session.query(key_col).limit(1).first() is not None
    except Exception:
        state["value"] = False
    state["checked_at"] = now
    return state["value"]


def normalize_phone(raw: str) -> str:
    """Match the normalisation used by the Dataform models.

    Strips whitespace/parens/dashes, converts AU ``+61…`` E.164 to local
    ``0…``, leaves anything else alone. Returns "" on empty/None input.
    """
    if not raw:
        return ""
    cleaned = re.sub(r"[\s()\-]", "", raw)
    if cleaned.startswith("+61"):
        return "0" + cleaned[3:]
    return cleaned


class Customer360Service:
    @property
    def client(self) -> bigquery.Client | None:
        # Reuse the singleton client created at app startup.
        return purchase_orders_service.client

    @staticmethod
    def _lookup_internal(phone: str) -> dict | None:
        """Return the InternalPhoneNumber row for ``phone``, or None.

        Wrapped in a try so a missing table (e.g. fresh dev DB before
        migration runs) downgrades to "not internal" rather than 500-ing.
        Lookup is by primary key on a 25-row table — sub-millisecond.
        """
        if not phone:
            return None
        try:
            from app.models.internal_phone import InternalPhoneNumber
            row = InternalPhoneNumber.query.get(phone)
        except Exception:
            return None
        if not row:
            return None
        return {
            "phone":     row.phone,
            "e164":      row.e164,
            "usage":     row.usage_type,
            "label":     row.label or "(unlabelled)",
            "extension": row.extension_number,
        }

    def get_card(self, raw_phone: str) -> dict:
        """Return the full payload for the customer card UI.

        Always returns a dict with the same shape so the template
        can render every block conditionally without needing to handle
        ``None`` at every level.
        """
        phone = normalize_phone(raw_phone)
        empty = {
            "phone": phone,
            "raw_phone": raw_phone,
            "matched": False,
            "usernames": [],
            "is_international": False,
            "is_internal_line": False,
            "internal_line": None,
            "customers": [],         # list of customer_360 rows (one per matching username)
            "call_history": None,    # call_history_360 row, or None
            "call_behavior": None,   # call_behavior_360 row, or None
            "error": None,
        }
        if not phone:
            empty["error"] = "Empty / unparseable phone number"
            return empty

        # --- Internal-line short-circuit ---
        # If this phone is one of JJ's own DIDs (IVR / staff direct line /
        # main company number / fax), we never want to render it as a
        # customer card. The matched-customer fallback would otherwise
        # pick whichever Neto record happens to have stored this number
        # by mistake (typically a typo or a "TEST DO NOT SEND" entry),
        # producing wildly inflated call counts and a meaningless name.
        internal = self._lookup_internal(phone)
        if internal:
            empty["is_internal_line"] = True
            empty["internal_line"] = internal
            return empty

        if self.client is None:
            empty["error"] = "BigQuery client not available"
            return empty

        # --- Query 1: the three phone-keyed tables in one round trip ---
        bundle = self._fetch_phone_bundle(phone)
        empty.update({
            "matched": bool(bundle["lookup"] and bundle["lookup"].get("usernames")),
            "usernames": (bundle["lookup"] or {}).get("usernames") or [],
            "is_international": (bundle["lookup"] or {}).get("is_international") or False,
            "call_history": bundle["history"],
            "call_behavior": bundle["behavior"],
        })

        # --- Query 2: customer_360 rows for any matched usernames ---
        if empty["usernames"]:
            empty["customers"] = self._fetch_customers(empty["usernames"])

        # --- Query 3: enrich SKUs in the card with their Neto product_id so
        # the template can link each SKU to the cpanel product page. We do
        # this as one bulk lookup rather than per-line. Cheap (~30 SKUs max
        # per card, single equality scan on neto_product_list).
        empty["product_id_by_sku"] = self._fetch_product_ids(self._collect_skus(empty["customers"]))

        # --- Live-merge: pull today's call_event rows for this phone and
        # blend them into the BQ-derived call_history snapshot, so a
        # customer who called back since last night's Dataform refresh
        # shows up correctly. By tomorrow morning the daily snapshot
        # picks them up and the merge becomes a no-op for those calls.
        empty["call_history"] = self._merge_today_calls_into_history(
            phone, empty["call_history"]
        )

        # --- Email history — pulled by customer email from
        # email_archive.messages (sales@ mailbox backfill).
        #
        # Gather EVERY distinct email address across every matched
        # customer record (multi-match: same phone → multiple Neto
        # records, often with different emails). Plus each record's
        # secondary_email when set. Querying the union catches
        # correspondence that lives under any of them — losing the
        # secondary email used to silently drop ~650 wholesale-
        # customer threads.
        emails: list[str] = []
        seen: set[str] = set()
        for cu in empty["customers"]:
            for fld in ("email", "secondary_email"):
                addr = (cu.get(fld) or "").strip().lower()
                if addr and addr not in seen:
                    seen.add(addr)
                    emails.append(addr)
        if emails:
            # Live top-up: pull anything sent/received since the last
            # hourly cron run, so a thread sent 5 minutes ago doesn't
            # silently drop off the panel. Soft-fails on Graph errors —
            # we just fall back to whatever the BQ snapshot has.
            self._live_topup(emails)
            empty["email_history"] = self._fetch_email_history(emails)
        else:
            empty["email_history"] = None

        return empty

    def _merge_today_calls_into_history(self, phone: str, history: dict | None) -> dict | None:
        """Augment the BQ call_history snapshot with today's call_event rows.

        ``history`` is the (possibly None) row from ``call_history_360`` that
        was loaded above. We:
          * dedupe today's events into one row per session (master+overlap)
          * bump the totals (total_calls, connected_total, etc.)
          * prepend today's sessions to recent_calls (newest first), capped
          * recompute last_call_date / days_since_last_call

        Tagged ``is_today: True`` per session so the template can mark them
        with a "today" badge if it wants.
        """
        today = self._build_today_call_entries(phone)
        if not today:
            return history

        h = dict(history) if history else {
            "phone": phone,
            "total_calls": 0,
            "pbx_total": 0,
            "cxone_total": 0,
            "inbound_total": 0,
            "outbound_total": 0,
            "connected_total": 0,
            "missed_total": 0,
            "abandoned_total": 0,
            "voicemail_total": 0,
            "refused_total": 0,
            "first_call_date": None,
            "last_call_date": None,
            "days_since_last_call": None,
            "recent_calls": [],
        }

        for call in today:
            h["total_calls"] = (h.get("total_calls") or 0) + 1
            src = (call.get("source") or "").lower()
            if src == "pbx":
                h["pbx_total"] = (h.get("pbx_total") or 0) + 1
            elif src == "cxone":
                h["cxone_total"] = (h.get("cxone_total") or 0) + 1
            direction = call.get("direction") or ""
            if direction == "Inbound":
                h["inbound_total"] = (h.get("inbound_total") or 0) + 1
            elif direction == "Outbound":
                h["outbound_total"] = (h.get("outbound_total") or 0) + 1
            disp = (call.get("disposition") or "").lower()
            for key in ("connected", "missed", "abandoned", "voicemail", "refused"):
                if disp == key:
                    h[f"{key}_total"] = (h.get(f"{key}_total") or 0) + 1

        # Sort today's newest-first and prepend; keep most recent 100 overall
        # (matches the BQ array cap so the template scrolls consistently).
        today_sorted = sorted(today, key=lambda c: c.get("call_time") or 0, reverse=True)
        existing = h.get("recent_calls") or h.get("last_5_calls") or []
        h["recent_calls"] = (today_sorted + existing)[:100]

        # Recency
        from datetime import date
        today_dt = date.today()
        if not h.get("first_call_date"):
            h["first_call_date"] = today_dt
        h["last_call_date"] = today_dt
        h["days_since_last_call"] = 0

        return h

    def _build_today_call_entries(self, phone: str) -> list[dict]:
        """Read today's ``call_event`` rows for ``phone`` and produce one
        entry per session, in the same shape as the BQ ``last_5_calls``
        struct so the template renders both kinds identically.

        Phone matched in either AU local or +61 form; events from the past
        24 hours considered "today" for the purposes of merging.
        """
        from urllib.parse import parse_qs
        from datetime import datetime, timedelta
        from sqlalchemy import or_

        from app.models.call_events import CallEvent

        if not phone:
            return []
        e164 = "+61" + phone[1:] if phone.startswith("0") and len(phone) >= 2 else phone

        since = datetime.utcnow() - timedelta(hours=24)
        events = (
            CallEvent.query
            .filter(CallEvent.received_at > since)
            .filter(CallEvent.session_id.isnot(None))
            .filter(or_(CallEvent.from_number == phone, CallEvent.from_number == e164))
            .order_by(CallEvent.received_at.asc())  # ASC so first event = call start
            .all()
        )
        if not events:
            return []

        # Group by master_session_id when present (CXone transfers share
        # this) — falls back to session_id for PBX events. Without this
        # collapse a single warm-transferred call would appear twice in
        # today's call history strip.
        sessions: dict[str, list] = {}
        for e in events:
            key = e.master_session_id or e.session_id
            sessions.setdefault(key, []).append(e)

        out: list[dict] = []
        for sid, sess_events in sessions.items():
            first = sess_events[0]
            last = sess_events[-1]

            # Determine disposition: connected if we ever saw Answered/Hold;
            # voicemail if we saw it; otherwise missed/abandoned if disconnected.
            saw_answered = any(e.event_type and ("answered" in e.event_type.lower() or "hold" in e.event_type.lower()) for e in sess_events)
            saw_voicemail = any(e.event_type and "voicemail" in (e.event_type or "").lower() for e in sess_events)
            terminal = last.event_type and "disconnected" in last.event_type.lower()

            if saw_answered:
                disposition = "connected"
            elif saw_voicemail:
                disposition = "voicemail"
            elif terminal:
                disposition = "missed"
            else:
                # Still in flight; mark as connected so the count card stays
                # in the green column. The drawer/active-call panel handles
                # the in-flight UX separately.
                disposition = "connected"

            duration_s = int((last.received_at - first.received_at).total_seconds())
            direction = (first.event_type or "").split(":", 1)[0] if ":" in (first.event_type or "") else "Inbound"

            # Pull agent name from latest event's body if it's there
            agent_name = None
            try:
                raw = last.body_json or ""
                if raw.startswith("{"):
                    import json as _json
                    body = _json.loads(raw)
                else:
                    parsed = parse_qs(raw, keep_blank_values=True)
                    body = {k: (v[0] if v else "") for k, v in parsed.items()}
                agent_name = (body.get("agentName") or "").strip() or None
            except Exception:
                pass

            from app.template_filters import utc_to_mel_naive
            out.append({
                # Use the actual contactId of the most recent leg (rather
                # than the master_id used for grouping) so the call-details
                # modal can look up the AI analysis row in BigQuery, which
                # is keyed by contactId.
                "session_id":       last.session_id,
                # call_event.received_at is naive UTC; the template filter
                # convention is "naive = already Mel", so shift here.
                "call_time":        utc_to_mel_naive(first.received_at),
                "_call_end":        utc_to_mel_naive(last.received_at),
                "direction":        direction,
                "disposition":      disposition,
                "duration_seconds": duration_s,
                "source":           first.source,
                "agent_name":       agent_name,
                "is_today":         True,
                "is_active":        not terminal,
            })

        # Cross-platform overlap merge: a single physical call can ring on
        # CXone briefly then forward to a PBX line. The two legs have
        # different session_ids and no shared masterContactId, but their
        # time windows overlap. Walk in start-time order and merge any leg
        # whose start is within 10s of the running max end-time.
        return _merge_overlapping_legs(out)

    def _collect_skus(self, customers: list[dict]) -> list[str]:
        """Walk the customer rows and gather every distinct SKU we'll display."""
        skus: set[str] = set()
        for cust in customers or []:
            for src in (
                cust.get("recent_order_lines") or [],
                cust.get("top_items") or [],
                cust.get("last_rma_lines") or [],
            ):
                for item in src:
                    sku = (item or {}).get("sku")
                    if sku:
                        skus.add(sku)
        return list(skus)

    # ------------------------------------------------------------------
    # Active call lookup — for the "Call in progress" panel
    # ------------------------------------------------------------------

    def prewarm(self, raw_phone: str) -> dict:
        """Pre-fetch the data the customer card will need, in advance of
        an agent clicking it.

        Called from the live-call webhook the moment a call_event lands
        so we can warm the slowest part of the card-load path — the
        Microsoft Graph live email top-up — while the call is still
        ringing. By the time the agent opens the card, _live_topup is
        a no-op (or very nearly so).

        Idempotent within ``_PREWARM_TTL`` seconds per phone: webhook
        bursts (ring + answer + transfer in quick succession) collapse
        into a single warm call. Returns a small status dict for
        debugging only.
        """
        phone = normalize_phone(raw_phone)
        if not phone:
            return {"prewarmed": False, "reason": "empty phone"}
        # Skip JJ-internal lines — they never resolve to a customer.
        if self._lookup_internal(phone):
            return {"prewarmed": False, "reason": "internal phone"}

        now = time.time()
        last = _PREWARM_RECENT.get(phone, 0)
        if now - last < _PREWARM_TTL:
            return {"prewarmed": False, "reason": "deduped"}
        _PREWARM_RECENT[phone] = now

        try:
            bundle = self._fetch_phone_bundle(phone)
            usernames = (bundle.get("lookup") or {}).get("usernames") or []
            if not usernames:
                log.info("prewarm phone=%s no-match (not in phone_lookup)", phone)
                return {"prewarmed": True, "phone": phone, "matched": False}
            customers = self._fetch_customers(usernames)
            seen: set[str] = set()
            emails: list[str] = []
            for cu in customers:
                for fld in ("email", "secondary_email"):
                    addr = (cu.get(fld) or "").strip().lower()
                    if addr and addr not in seen:
                        seen.add(addr)
                        emails.append(addr)
            if emails:
                self._live_topup(emails)
            log.info("prewarm phone=%s usernames=%d emails=%d",
                     phone, len(usernames), len(emails))
            return {"prewarmed": True, "phone": phone, "emails": len(emails)}
        except Exception as exc:
            log.warning("prewarm failed for %s: %s", phone, exc)
            return {"prewarmed": False, "reason": str(exc)}

    def get_active_call_for_phone(self, raw_phone: str) -> dict | None:
        """If this phone has a currently in-flight call_event, return its
        details (session, latest event, agent name, skill, dialed number).

        Reads from the local SQLite ``call_event`` table populated by
        webhook deliveries (RC PBX) and the cxone-poller daemon. Matches
        the phone in either AU local (``04…``) or E.164 (``+61…``) form
        because the two sources don't always agree on which one they emit.

        Returns ``None`` when no active call is found.
        """
        from urllib.parse import parse_qs
        from datetime import datetime, timedelta
        from sqlalchemy import or_

        from app.models.call_events import CallEvent

        phone = normalize_phone(raw_phone)
        if not phone:
            return None
        e164 = "+61" + phone[1:] if phone.startswith("0") and len(phone) >= 2 else phone

        since = datetime.utcnow() - timedelta(minutes=10)
        events = (
            CallEvent.query
            .filter(CallEvent.received_at > since)
            .filter(CallEvent.session_id.isnot(None))
            .filter(or_(CallEvent.from_number == phone, CallEvent.from_number == e164))
            .order_by(CallEvent.received_at.desc())
            .all()
        )
        if not events:
            return None

        # Group by master_session_id (CXone transferred legs share this) so
        # the panel doesn't flicker between two different agent names when
        # a warm transfer is happening — we display the currently-active
        # leg only. PBX events have NULL master and fall through to
        # session_id, so they're unaffected.
        latest_per_master: dict[str, "CallEvent"] = {}
        earliest_at: dict[str, datetime] = {}
        for e in events:
            key = e.master_session_id or e.session_id
            if key not in latest_per_master:
                latest_per_master[key] = e  # rows came in DESC, first wins
            if key not in earliest_at or e.received_at < earliest_at[key]:
                earliest_at[key] = e.received_at

        # Find a master whose latest event is NOT a Disconnected
        for sid, latest in latest_per_master.items():
            if latest.event_type and "disconnected" in latest.event_type.lower():
                continue

            # Pull agentName / skill out of the body — the CXone poller posts
            # form-encoded so body_json is `key=value&key=value`.
            body: dict = {}
            raw = latest.body_json or ""
            if raw.startswith("{"):
                # Some sources may post JSON; try that first.
                import json as _json
                try:
                    body = _json.loads(raw)
                except Exception:
                    pass
            elif "=" in raw:
                parsed = parse_qs(raw, keep_blank_values=True)
                body = {k: (v[0] if v else "") for k, v in parsed.items()}

            # event_type is composite "Direction:Status" — split for the badge
            status_code = (latest.event_type or "").split(":", 1)[-1]
            direction = (latest.event_type or "").split(":", 1)[0] if ":" in (latest.event_type or "") else None
            elapsed = int((datetime.utcnow() - earliest_at[sid]).total_seconds()) if earliest_at[sid] else 0
            return {
                "session_id":     sid,
                "event_type":     latest.event_type,
                "status_code":    status_code,
                "direction":      direction,
                "from_number":    latest.from_number,
                "to_number":      latest.to_number,
                "source":         latest.source,
                "agent_name":     (body.get("agentName") or "").strip() or None,
                "skill":          body.get("skill") or None,
                "media_type":     body.get("mediaTypeName") or None,
                "started_at":     earliest_at[sid],
                "elapsed_seconds": elapsed,
                "is_ringing":     status_code in ("Setup", "Proceeding", "Alerting"),
                "is_connected":   status_code in ("Answered", "Hold"),
            }

        return None

    def _fetch_product_ids(self, skus: list[str]) -> dict[str, str]:
        """Bulk SKU → Neto product ID lookup. Cache-first, BQ fallback."""
        if not skus:
            return {}
        if _cache_table_ready(CachedNetoProduct, CachedNetoProduct.sku):
            rows = (db.session.query(CachedNetoProduct.sku, CachedNetoProduct.product_id)
                    .filter(CachedNetoProduct.sku.in_(skus))
                    .all())
            return {sku: pid for sku, pid in rows if sku and pid}
        if self.client is None:
            return {}
        sql = f"""
        SELECT SKU, ID
        FROM `{PROJECT}.{DATASET}.neto_product_list`
        WHERE SKU IN UNNEST(@skus)
        """
        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("skus", "STRING", skus)]
            ),
        )
        return {row.SKU: row.ID for row in job.result() if row.SKU and row.ID}

    # ------------------------------------------------------------------
    # Internal queries
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Lightweight phone → name lookup for the live-calls drawer
    # ------------------------------------------------------------------
    #
    # The drawer polls every 3s; we don't want to round-trip BigQuery for
    # the same phone over and over. ``lru_cache`` keeps results in-process
    # for the lifetime of the worker. ``None`` results are cached too so
    # unknown numbers don't re-query forever. If a customer's name changes
    # in Neto, restart the service to bust the cache (rare).

    @functools.lru_cache(maxsize=4096)
    def get_name_for_phone(self, raw_phone: str) -> Optional[str]:
        """Return ``"First Last"`` for a phone, or ``None`` if no match.

        Resolves phone → username via the customer-cache lookup and picks
        the highest-lifetime-value customer when multiple records share
        the number (household / repeat-guest case). Falls back to BQ if
        the cache is empty.
        """
        phone = normalize_phone(raw_phone)
        if not phone:
            return None
        # Phone-lookup cached but customer_360 NOT cached is the common
        # path now (Option A3): we know the username from phone_lookup
        # but still need a BQ round-trip for the name. The "both cached"
        # path stays as a fast-future-state for when we add customer_360
        # caching back in Phase 2.
        pl_ready = _cache_table_ready(CachedPhoneLookup, CachedPhoneLookup.phone)
        c360_ready = _cache_table_ready(CachedCustomer360, CachedCustomer360.Username)
        if pl_ready and c360_ready:
            pl = db.session.query(CachedPhoneLookup).filter_by(phone=phone).first()
            if not pl:
                return None
            usernames = json.loads(pl.usernames_json)
            if not usernames:
                return None
            rows = (db.session.query(CachedCustomer360.payload_json)
                    .filter(CachedCustomer360.Username.in_(usernames))
                    .all())
            customers = [json.loads(r.payload_json) for r in rows]
            if not customers:
                return None
            customers.sort(key=_lifetime_value_key, reverse=True)
            top = customers[0]
            full = ((top.get("name_first") or "") + " "
                    + (top.get("name_last") or "")).strip()
            return full or None
        if self.client is None:
            return None
        sql = f"""
        WITH matched_usernames AS (
          SELECT u AS username
          FROM `{PROJECT}.{DATASET}.customer_phone_lookup`,
               UNNEST(usernames) AS u
          WHERE phone = @phone
        )
        SELECT c.name_first, c.name_last
        FROM matched_usernames m
        JOIN `{PROJECT}.{DATASET}.customer_360` c ON c.Username = m.username
        ORDER BY COALESCE(c.lifetime_value, 0) DESC
        LIMIT 1
        """
        try:
            job = self.client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("phone", "STRING", phone)]
                ),
            )
            row = next(iter(job.result()), None)
        except Exception:
            return None
        if row is None:
            return None
        first = (row.name_first or "").strip()
        last = (row.name_last or "").strip()
        full = (first + " " + last).strip()
        return full or None

    def _fetch_phone_bundle(self, phone: str) -> dict:
        # Phone bundle is gated on phone_lookup being loaded — if that's
        # there, call_history and call_behavior are too (same refresh
        # transaction). Single readiness probe avoids 3 round-trips.
        if _cache_table_ready(CachedPhoneLookup, CachedPhoneLookup.phone):
            return self._fetch_phone_bundle_from_cache(phone)
        return self._fetch_phone_bundle_from_bq(phone)

    def _fetch_phone_bundle_from_cache(self, phone: str) -> dict:
        pl = db.session.query(CachedPhoneLookup).filter_by(phone=phone).first()
        ch = db.session.query(CachedCallHistory).filter_by(phone=phone).first()
        cb = db.session.query(CachedCallBehavior).filter_by(phone=phone).first()
        lookup = None
        if pl:
            lookup = {
                "phone":            pl.phone,
                "usernames":        json.loads(pl.usernames_json),
                "match_count":      pl.match_count,
                "is_international": pl.is_international,
            }
        return {
            "lookup":   lookup,
            "history":  json.loads(ch.payload_json) if ch else None,
            "behavior": json.loads(cb.payload_json) if cb else None,
        }

    def _fetch_phone_bundle_from_bq(self, phone: str) -> dict:
        if self.client is None:
            return {"lookup": None, "history": None, "behavior": None}
        sql = f"""
        SELECT
          (SELECT AS STRUCT *
           FROM `{PROJECT}.{DATASET}.customer_phone_lookup`
           WHERE phone = @phone) AS phone_match,
          (SELECT AS STRUCT *
           FROM `{PROJECT}.{DATASET}.call_history_360`
           WHERE phone = @phone) AS calls,
          (SELECT AS STRUCT *
           FROM `{PROJECT}.{DATASET}.call_behavior_360`
           WHERE phone = @phone) AS insights
        """
        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("phone", "STRING", phone)]
            ),
        )
        row = next(iter(job.result()), None)
        if row is None:
            return {"lookup": None, "history": None, "behavior": None}
        return {
            "lookup":   _row_to_dict(row.phone_match),
            "history":  _row_to_dict(row.calls),
            "behavior": _row_to_dict(row.insights),
        }

    def _fetch_customers(self, usernames: list[str]) -> list[dict]:
        if not usernames:
            return []
        if _cache_table_ready(CachedCustomer360, CachedCustomer360.Username):
            rows = (db.session.query(CachedCustomer360)
                    .filter(CachedCustomer360.Username.in_(usernames))
                    .all())
            customers = [json.loads(r.payload_json) for r in rows]
            customers.sort(key=_lifetime_value_key, reverse=True)
            return customers
        if self.client is None:
            return []
        sql = f"""
        SELECT *
        FROM `{PROJECT}.{DATASET}.customer_360`
        WHERE Username IN UNNEST(@usernames)
        ORDER BY COALESCE(lifetime_value, 0) DESC
        """
        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("usernames", "STRING", usernames)
                ]
            ),
        )
        return [_row_to_dict(r) for r in job.result()]

    # ------------------------------------------------------------------
    # Email history — pulled from email_archive.messages
    # ------------------------------------------------------------------

    def _fetch_email_history(self, customer_emails) -> dict | None:
        """Return per-customer email aggregates + the most recent 50
        messages, or None if no addresses given.

        Cache-first (Phase 2b): SQLite mirror of email_archive.messages
        gives us sub-50ms reads from the Sydney VPS. Falls back to
        BigQuery if the cache hasn't been loaded yet (the first run,
        or anytime the email cache is empty).
        """
        if isinstance(customer_emails, str):
            customer_emails = [customer_emails]
        emails = [e.strip().lower() for e in (customer_emails or []) if e and e.strip()]
        if not emails:
            return None
        if _cache_table_ready(CachedEmailMessage, CachedEmailMessage.message_id):
            return self._fetch_email_history_from_cache(emails)
        return self._fetch_email_history_from_bq(emails)

    def _fetch_email_history_from_cache(self, emails: list[str]) -> dict | None:
        """SQLite path — match emails against from_address OR any
        recipient address, then aggregate."""
        # Resolve matching message_ids in two indexed scans, union the
        # set in Python (cheap because the typical match is tens to
        # low hundreds of message_ids).
        from_ids = [m for (m,) in db.session.query(CachedEmailMessage.message_id)
                    .filter(CachedEmailMessage.from_address.in_(emails)).all()]
        rcp_ids = [m for (m,) in db.session.query(CachedEmailRecipient.message_id)
                   .filter(CachedEmailRecipient.address.in_(emails)).all()]
        match_ids = list({*from_ids, *rcp_ids})
        if not match_ids:
            rows = []
        else:
            rows = (db.session.query(CachedEmailMessage)
                    .filter(CachedEmailMessage.message_id.in_(match_ids))
                    .order_by(CachedEmailMessage.received_at.desc())
                    .all())

        total = len(rows)
        received_total = sum(1 for r in rows if r.direction == "inbound")
        sent_total = sum(1 for r in rows if r.direction == "outbound")
        automated = sum(1 for r in rows if r.is_automated)
        with_attachments = sum(1 for r in rows if r.has_attachments)
        last_at = rows[0].received_at if rows else None
        days_since_last = None
        if last_at:
            from datetime import date
            try:
                days_since_last = (date.today() - last_at.date()).days
            except Exception:
                days_since_last = None

        msgs = [{
            "message_id":         r.message_id,
            "conversation_id":    r.conversation_id,
            "direction":          r.direction,
            "subject":            r.subject,
            "from_address":       r.from_address,
            "from_name":          r.from_name,
            "received_at":        r.received_at.isoformat() if r.received_at else None,
            "is_automated":       r.is_automated,
            "has_attachments":    r.has_attachments,
            "parent_folder_name": r.parent_folder_name,
            "web_link":           r.web_link,
            "body_preview":       r.body_preview,
        } for r in rows[:50]]

        return {
            "email":            emails[0],
            "emails_checked":   emails,
            "total":            total,
            "received_total":   received_total,
            "sent_total":       sent_total,
            "automated_total":  automated,
            "with_attachments": with_attachments,
            "last_at":          last_at.isoformat() if last_at else None,
            "days_since_last":  days_since_last,
            "messages":         msgs,
        }

    def _fetch_email_history_from_bq(self, emails: list[str]) -> dict | None:
        """BQ fallback — kept for the period before the email cache is
        first loaded, and as a safety net if the cache is unavailable."""
        if self.client is None:
            return None
        sql = """
        WITH matched AS (
          SELECT * FROM (
            SELECT *,
              ROW_NUMBER() OVER (
                PARTITION BY message_id
                ORDER BY ingested_at DESC
              ) AS _rn
            FROM `chainsawspares-385722.email_archive.messages`
            WHERE LOWER(from_address) IN UNNEST(@emails)
               OR EXISTS (
                    SELECT 1 FROM UNNEST(to_addresses) AS t
                    WHERE LOWER(t) IN UNNEST(@emails)
               )
          )
          WHERE _rn = 1
        )
        SELECT
          (SELECT AS STRUCT
             COUNT(*) AS total,
             COUNTIF(direction = 'inbound')  AS received,
             COUNTIF(direction = 'outbound') AS sent,
             COUNTIF(is_automated)           AS automated,
             COUNTIF(has_attachments)        AS with_attachments,
             MAX(received_at) AS last_at,
             DATE_DIFF(CURRENT_DATE('Australia/Melbourne'),
                       DATE(MAX(received_at)), DAY) AS days_since_last
           FROM matched) AS aggs,
          ARRAY(
            SELECT AS STRUCT
              message_id, conversation_id, direction, subject,
              from_address, from_name,
              received_at, is_automated, has_attachments,
              parent_folder_name, web_link,
              SUBSTR(body_preview, 1, 200) AS body_preview
            FROM matched
            ORDER BY received_at DESC
            LIMIT 50
          ) AS messages
        """
        try:
            job = self.client.query(
                sql,
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ArrayQueryParameter("emails", "STRING", emails)
                ]),
            )
            row = next(iter(job.result()), None)
        except Exception:
            # BQ failure (e.g. table missing during dev) — surface as
            # "no panel" rather than fake zeros, so we don't claim we
            # checked when we couldn't.
            return None
        if not row:
            return None
        aggs = _row_to_dict(row.aggs) or {}
        msgs = [_row_to_dict(m) for m in (row.messages or [])]
        # Always return the dict — even with zero messages — so the
        # template renders an empty panel that tells the agent "we
        # checked, there's no email correspondence" rather than just
        # silently omitting the section.
        return {
            # ``email`` kept for backward-compat with the template's empty-
            # state copy; it's the first address we checked. The full set
            # we queried is in ``emails_checked``.
            "email":            emails[0],
            "emails_checked":   emails,
            "total":            aggs.get("total") or 0,
            "received_total":   aggs.get("received") or 0,
            "sent_total":       aggs.get("sent") or 0,
            "automated_total":  aggs.get("automated") or 0,
            "with_attachments": aggs.get("with_attachments") or 0,
            "last_at":          aggs.get("last_at"),
            "days_since_last":  aggs.get("days_since_last"),
            "messages":         msgs,
        }

    # ------------------------------------------------------------------
    # Live email top-up — closes the freshness gap between hourly cron
    # runs. On each card load we ask Graph for the most recent ~10
    # messages per customer email, dedupe against BQ, and stream the
    # delta in. Latency budget: ~700ms typical, ~1.5s worst case.
    # Soft-fails on any error — card load must never break because Graph
    # is slow or auth tripped.
    # ------------------------------------------------------------------

    def _live_topup(self, customer_emails: list[str]) -> int:
        if not customer_emails or self.client is None:
            return 0
        try:
            from urllib.parse import quote
            from datetime import datetime, timezone
            # Lazy-import — scripts/ is on sys.path via module-level
            # path tweak. Same module the systemd timer runs.
            from email_backfill import (  # type: ignore
                get_token, graph_get, to_row, insert_batch, MAILBOX, FIELDS,
            )

            now = time.time()
            # 50-min token TTL (Graph tokens last 60min — buffer).
            tok = _GRAPH_TOKEN["value"]
            if not tok or now > _GRAPH_TOKEN["expires_at"]:
                tok = get_token()
                _GRAPH_TOKEN["value"] = tok
                _GRAPH_TOKEN["expires_at"] = now + 50 * 60
            # Skip list_folders here — that's a ~2s paginated walk and
            # we don't strictly need folder names for live-topped-up
            # rows. They'll have parent_folder_name=NULL; the next
            # hourly cron walks from a saved watermark and would re-
            # insert each message, but row_ids dedup in insert_batch
            # silently drops the dupe so the NULL stays. Trade: a small
            # subset of rows show no folder badge in the UI; the win
            # is sub-300ms cold-start latency.
            fmap: dict = {}

            ingested_at = datetime.now(timezone.utc)

            def _search_one(em: str) -> list[dict]:
                # KQL participants: matches from/to/cc/bcc in one shot.
                # $search value must be wrapped in double quotes; encode
                # the whole thing and pass ConsistencyLevel:eventual as
                # the search endpoint requires.
                search_value = '"participants:' + em + '"'
                url = (f"https://graph.microsoft.com/v1.0/users/{quote(MAILBOX)}/messages"
                       f"?$search={quote(search_value)}&$top=10&$select={FIELDS}")
                try:
                    return graph_get(url, tok,
                                     extra_headers={"ConsistencyLevel": "eventual"}
                                     ).get("value", [])
                except Exception as exc:
                    log.warning("live top-up: Graph search failed for %s: %s", em, exc)
                    return []

            # Run searches in parallel — one round-trip per email is
            # ~500ms; serial costs N×500ms but parallel collapses to one.
            from concurrent.futures import ThreadPoolExecutor
            seen_ids: set[str] = set()
            new_msgs: list[dict] = []
            with ThreadPoolExecutor(max_workers=min(4, len(customer_emails))) as pool:
                for batch in pool.map(_search_one, customer_emails):
                    for m in batch:
                        mid = m.get("id")
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        new_msgs.append(m)

            if not new_msgs:
                return 0

            # No pre-filter against BQ — round-tripping a US-region table
            # from the Sydney VPS adds 1.5s for marginal benefit. Instead
            # we rely on insert_batch's row_ids=message_id dedup, which
            # silently drops dupes within a 60-min streaming window. The
            # next hourly cron walk catches anything older.
            to_insert = [to_row(m, MAILBOX, fmap, ingested_at) for m in new_msgs]
            n = insert_batch(self.client, to_insert)

            # Mirror into local SQLite cache so the message is visible
            # on the next card load without waiting for the next email
            # cache refresh. Soft-fail — BQ is the source of truth.
            try:
                from app.services.email_cache import upsert_message
                for row in to_insert:
                    upsert_message(row)
            except Exception as exc:
                log.warning("live top-up: SQLite mirror failed: %s", exc)
            return n
        except Exception as exc:
            log.warning("live email top-up failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Per-call detail: AI summary + transcript + classifications + audio
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Sensitivity flag — see app.models.call_sensitivity.CallSensitivityFlag
    # ------------------------------------------------------------------
    # Calls flagged "sensitive" (management portions, escalations, internal
    # handovers) are gated so only users with the
    # ``support.calls.view_sensitive`` capability see the analysis bundle
    # (summary, transcription, audio URL, classifications, sentiment).
    # Other users see the metadata header + a "restricted" banner.
    #
    # These helpers are pure data — they don't enforce auth. Auth is
    # enforced by ``require_capability`` on the route layer and by
    # :func:`redact_sensitive_call_details` below, which strips the
    # restricted fields from the payload before render.

    def is_call_sensitive(self, session_id: str) -> bool:
        """Return True iff a ``call_sensitivity_flag`` row exists."""
        if not session_id:
            return False
        from app.models.call_sensitivity import CallSensitivityFlag
        return CallSensitivityFlag.query.filter_by(
            session_id=session_id
        ).first() is not None

    def get_sensitivity_flag(self, session_id: str):
        """Return the ``CallSensitivityFlag`` row or None.

        Used by the modal to show "Flagged by Adil · 14 May, 10:30" when
        the viewer holds ``support.calls.view_sensitive``.
        """
        if not session_id:
            return None
        from app.models.call_sensitivity import CallSensitivityFlag
        return CallSensitivityFlag.query.filter_by(session_id=session_id).first()

    def set_call_sensitivity(
        self,
        session_id: str,
        sensitive: bool,
        user_id: int | None,
        reason: str | None = None,
    ) -> bool:
        """Flag / unflag a call. Returns the new ``is_sensitive`` value.

        Presence of a row == flagged. Unflagging deletes the row, which
        also drops the audit attribution (who flagged it last); that's
        fine for v1, an audit log table can be added later if needed.
        """
        from app.extensions import db
        from app.models.call_sensitivity import CallSensitivityFlag
        existing = CallSensitivityFlag.query.filter_by(session_id=session_id).first()
        if sensitive:
            if existing is None:
                row = CallSensitivityFlag(
                    session_id=session_id,
                    flagged_by_user_id=user_id,
                    reason=(reason or None),
                )
                db.session.add(row)
            else:
                # Re-flagging — update attribution and reason but don't
                # bump flagged_at (the original moment is more interesting
                # than the most-recent edit).
                existing.flagged_by_user_id = user_id
                if reason is not None:
                    existing.reason = reason or None
            db.session.commit()
            return True
        else:
            if existing is not None:
                db.session.delete(existing)
                db.session.commit()
            return False

    def get_call_details(self, session_id: str) -> dict:
        """Pull the full bundle for one historical call (for the modal).

        Sources:
          * recording_fetch_status — transcript, summary, sentiment, topics,
            intents, gcs_uri (audio file)
          * call_classifications  — structured AI fields (call_type, sale_result,
            problems_detected, etc.)
          * callsrep_rep_contacts_completed_v2 — call metadata (start/duration/agent)

        Always sets ``is_sensitive`` on the returned dict based on the
        ``call_sensitivity_flag`` table. Server-side redaction for users
        without ``support.calls.view_sensitive`` happens in the route
        layer via :func:`redact_sensitive_call_details`, not here — this
        function is a pure data fetcher.

        ``session_id`` matches CXone calls directly (numeric contactId stored
        as STRING in our last_5_calls). For PBX calls the session_id is the
        RC telephony session (``s-...``), which does NOT match
        ``recording_fetch_status.contactId`` (we store the RC ``recording_id``
        there). For PBX we therefore fall back to a phone+time lookup against
        ``account_call_log_leg`` to find the corresponding ``recording_id``
        and JOIN forward.
        """
        # Resolve the sensitivity flag once, up-front, so every code path
        # below sees a consistent value (every branch of get_call_details
        # ultimately returns a dict — empty, basic, or full — and they
        # all need is_sensitive on them).
        flag = self.get_sensitivity_flag(session_id)
        sens_fields = {
            "is_sensitive": flag is not None,
            "sensitivity_reason": (flag.reason if flag else None),
            "sensitivity_flagged_by": (
                (flag.flagged_by.display_name or flag.flagged_by.username)
                if flag and flag.flagged_by else None
            ),
            "sensitivity_flagged_at": (flag.flagged_at_local if flag else None),
        }

        empty = {
            "session_id": session_id,
            "found": False,
            "recording_signed_url": None,
            **sens_fields,
        }
        if not session_id or self.client is None:
            return empty

        sql = """
        SELECT
          r.contactId,
          r.summary,
          r.transcription,
          r.sentiment,
          r.topics,
          r.intents,
          r.gcs_uri,
          r.recording_filename,
          r.fetch_datetime,
          r.source AS recording_source,
          r.account_call_log_id,
          c.call_type,
          c.sale_result,
          c.product_family,
          c.product_category_detail,
          c.no_sale_reasons,
          c.problems_detected,
          c.escalation_actions,
          c.delivery_tracking,
          c.confidence_scores,
          c.agent_name,
          m.startDate AS cxone_start_date,
          m.agentSeconds AS cxone_duration_seconds,
          m.endReason AS cxone_end_reason,
          m.skillName,
          m.firstName  AS agent_first_name,
          m.lastName   AS agent_last_name
        FROM `chainsawspares-385722.ringcentral_jnj.recording_fetch_status` r
        LEFT JOIN `chainsawspares-385722.ringcentral_jnj.call_classifications` c
          ON r.contactId = c.contactId
        LEFT JOIN `chainsawspares-385722.ringcentral_jnj.callsrep_rep_contacts_completed_v2` m
          ON SAFE_CAST(r.contactId AS NUMERIC) = m.contactId
        WHERE r.contactId = @session_id
        ORDER BY r.fetch_datetime DESC
        LIMIT 1
        """
        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("session_id", "STRING", session_id)]
            ),
        )
        row = next(iter(job.result()), None)

        # If nothing found directly AND the session_id looks like a PBX one
        # (the RC telephony session-id format starts with "s-"), try to find
        # the matching PBX recording via phone+time lookup in the call log.
        if row is None and isinstance(session_id, str) and session_id.startswith("s-"):
            row = self._lookup_pbx_recording_via_call_log(session_id)

        # If still nothing, fall back to the local call_event log — covers
        # today's calls (analyzer hasn't run yet) and sessions that never
        # produced a recording at all. The fallback path merges sens_fields
        # in itself (see ``_call_details_from_event_log``).
        if row is None:
            basic = self._call_details_from_event_log(session_id)
            basic.update(sens_fields)
            return basic

        d = _row_to_dict(row)
        d["session_id"] = session_id
        d["found"] = True

        # Parse the JSON-string sentiment / topics / intents columns into real
        # structures so the template can render them without re-parsing.
        for col in ("sentiment", "topics", "intents"):
            raw = d.get(col)
            if isinstance(raw, str) and raw:
                try:
                    import json as _json
                    d[col + "_parsed"] = _json.loads(raw)
                except Exception:
                    d[col + "_parsed"] = None
            else:
                d[col + "_parsed"] = None

        # Generate a signed URL for the audio (15 min) so the <audio> tag
        # in the modal can play directly.
        if d.get("gcs_uri"):
            d["recording_signed_url"] = self._sign_gcs_url(d["gcs_uri"], minutes=15)

        # Sensitivity attribution. Templates read these; ``is_sensitive``
        # also drives the redaction in :func:`redact_sensitive_call_details`.
        d.update(sens_fields)
        return d

    def _lookup_pbx_recording_via_call_log(self, session_id: str):
        """For a PBX telephony session_id (``s-...``), find the matching row
        in ``recording_fetch_status`` via a deterministic JOIN.

        ``ringcentral.account_call_log_leg.telephony_session_id`` is the
        same ``s-...`` identifier that RC sends in its Telephony webhook
        events (and that Customer 360 stores on ``CallEvent.session_id``).
        So:

            session_id                      (e.g. "s-a035f291...")
              == account_call_log_leg.telephony_session_id
                 → gives us account_call_log_leg.recording_id
                    == recording_fetch_status.contactId  (for source='pbx')

        No phone/time fuzziness — it's a direct key-to-key match.
        Returns a BigQuery ``Row`` or ``None``.
        """
        sql = """
        WITH matched AS (
          -- A telephony_session_id is shared by every leg of a call;
          -- pick the one leg that holds a recording_id.
          SELECT
            ANY_VALUE(recording_id)         AS recording_id,
            ANY_VALUE(account_call_log_id)  AS account_call_log_id
          FROM `chainsawspares-385722.ringcentral.account_call_log_leg`
          WHERE telephony_session_id = @session_id
            AND recording_id IS NOT NULL AND recording_id != ''
        )
        SELECT
          r.contactId,
          r.summary,
          r.transcription,
          r.sentiment,
          r.topics,
          r.intents,
          r.gcs_uri,
          r.recording_filename,
          r.fetch_datetime,
          r.source AS recording_source,
          r.account_call_log_id,
          c.call_type,
          c.sale_result,
          c.product_family,
          c.product_category_detail,
          c.no_sale_reasons,
          c.problems_detected,
          c.escalation_actions,
          c.delivery_tracking,
          c.confidence_scores,
          c.agent_name,
          CAST(NULL AS TIMESTAMP) AS cxone_start_date,
          CAST(NULL AS INT64)     AS cxone_duration_seconds,
          CAST(NULL AS STRING)    AS cxone_end_reason,
          CAST(NULL AS STRING)    AS skillName,
          CAST(NULL AS STRING)    AS agent_first_name,
          CAST(NULL AS STRING)    AS agent_last_name
        FROM matched m
        JOIN `chainsawspares-385722.ringcentral_jnj.recording_fetch_status` r
          ON r.contactId = m.recording_id AND r.source = 'pbx'
        LEFT JOIN `chainsawspares-385722.ringcentral_jnj.call_classifications` c
          ON r.contactId = c.contactId
        WHERE m.recording_id IS NOT NULL
        LIMIT 1
        """
        job = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
                ]
            ),
        )
        return next(iter(job.result()), None)

    def _call_details_from_event_log(self, session_id: str) -> dict:
        """Fallback for the call-detail modal: summarise a session from
        ``call_event`` when the analyzer hasn't picked it up yet.

        Returns the same shape as ``get_call_details`` so the modal
        template can branch on ``found`` and ``has_analysis`` to decide
        what to render.
        """
        from urllib.parse import parse_qs
        from app.models.call_events import CallEvent

        events = (
            CallEvent.query
            .filter(CallEvent.session_id == session_id)
            .order_by(CallEvent.received_at.asc())
            .all()
        )
        if not events:
            # Last fallback: CXone metadata table. Has every CXone call
            # back to 2023, including ones that predate our AI analyzer
            # pipeline (which started 2026-02-19) and aren't in the local
            # call_event log.
            return self._call_details_from_cxone_metadata(session_id)

        first, last = events[0], events[-1]
        saw_answered = any(e.event_type and "answered" in e.event_type.lower() for e in events)
        terminal = last.event_type and "disconnected" in last.event_type.lower()

        # Pull a few fields from the body_json if it's form-encoded
        agent_name = None
        skill = None
        try:
            raw = last.body_json or ""
            if raw.startswith("{"):
                import json as _json
                body = _json.loads(raw)
            else:
                parsed = parse_qs(raw, keep_blank_values=True)
                body = {k: (v[0] if v else "") for k, v in parsed.items()}
            agent_name = (body.get("agentName") or "").strip() or None
            skill = body.get("skill") or None
        except Exception:
            pass

        duration_s = int((last.received_at - first.received_at).total_seconds())
        return {
            "session_id":   session_id,
            "found":        True,
            "has_analysis": False,            # signals "no transcript yet" branch
            "source":       first.source,
            "from_number":  first.from_number,
            "to_number":    first.to_number,
            "started_at":   first.received_at,
            "ended_at":     last.received_at if terminal else None,
            "duration_seconds": duration_s,
            "agent_name":   agent_name,
            "skill":        skill,
            "first_event_type": first.event_type,
            "last_event_type":  last.event_type,
            "is_active":    not terminal,
            "saw_answered": saw_answered,
            "event_count":  len(events),
        }

    def _call_details_from_cxone_metadata(self, session_id: str) -> dict:
        """Last-resort fallback: pull basic call metadata from the
        ``callsrep_rep_contacts_completed_v2`` table.

        Covers calls older than the AI analyzer pipeline cutoff
        (~2026-02-19) that are too old to be in the local ``call_event``
        log either. Returns the same dict shape as
        :meth:`_call_details_from_event_log` so the modal's
        ``has_analysis is false`` branch renders directly.
        """
        if not session_id or self.client is None:
            return {"session_id": session_id, "found": False}
        sql = f"""
        SELECT
          contactId,
          startDate,
          agentSeconds,
          endReason,
          skillName,
          firstName,
          lastName
        FROM `chainsawspares-385722.ringcentral_jnj.callsrep_rep_contacts_completed_v2`
        WHERE contactId = SAFE_CAST(@session_id AS NUMERIC)
        LIMIT 1
        """
        try:
            row = next(iter(self.client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("session_id", "STRING", session_id)]
                ),
            ).result()), None)
        except Exception:
            return {"session_id": session_id, "found": False}
        if row is None:
            return {"session_id": session_id, "found": False}

        agent_name = " ".join(filter(None, [
            (row.firstName or "").strip(),
            (row.lastName or "").strip(),
        ])).strip() or None
        return {
            "session_id":        session_id,
            "found":             True,
            "has_analysis":      False,
            "source":            "cxone",
            "from_number":       None,
            "to_number":         None,
            "started_at":        row.startDate,
            "ended_at":          None,
            "duration_seconds":  row.agentSeconds,
            "agent_name":        agent_name,
            "skill":             row.skillName,
            "first_event_type":  None,
            "last_event_type":   row.endReason,
            "is_active":         False,
            "saw_answered":      bool(row.agentSeconds and row.agentSeconds > 0),
            "event_count":       0,
            # Flag so the modal can render "predates AI analyzer" copy
            # rather than the default "analyzer hasn't run yet, refresh
            # tomorrow" message.
            "predates_analyzer": True,
        }

    def _sign_gcs_url(self, gcs_uri: str, minutes: int = 15) -> str | None:
        """Sign a ``gs://bucket/object`` path for short-lived public access.

        Reuses the same service-account credentials BigQuery is using.
        Returns ``None`` if the URL can't be parsed or the storage client
        isn't reachable.
        """
        if not gcs_uri or not gcs_uri.startswith("gs://"):
            return None
        try:
            from datetime import timedelta
            from google.cloud import storage
            # Pull credentials from the live BQ client so we don't re-load them.
            creds = getattr(self.client, "_credentials", None)
            project = getattr(self.client, "project", PROJECT)
            stor = storage.Client(credentials=creds, project=project)
            bucket_name, _, blob_name = gcs_uri[5:].partition("/")
            blob = stor.bucket(bucket_name).blob(blob_name)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=minutes),
                method="GET",
            )
        except Exception as e:
            # Surface to logs but don't blow up the modal — audio is optional.
            import logging
            logging.getLogger(__name__).warning("GCS sign failed for %s: %s", gcs_uri, e)
            return None


# Global singleton, mirroring purchase_orders_service.
customer_360_service = Customer360Service()


# ---------------------------------------------------------------------------
# Sensitive-call redaction (defense in depth)
# ---------------------------------------------------------------------------
# The call-details modal template ALSO gates these fields via ``can(...)``,
# but redacting them here guarantees they never reach the rendering path
# for a user without ``support.calls.view_sensitive`` — so a future
# template change can't accidentally leak them. The route layer calls
# this immediately after :func:`Customer360Service.get_call_details`.

# Fields stripped when a call is flagged sensitive and the viewer lacks
# ``support.calls.view_sensitive``. Header metadata (agent, duration,
# skill, end reason, contactId, start date) is deliberately KEPT so the
# basic-info view still works.
_SENSITIVE_FIELDS: tuple[str, ...] = (
    # Raw analysis content
    "summary",
    "transcription",
    # Audio playback — both the signed URL and the source pointer
    "recording_signed_url",
    "gcs_uri",
    "recording_filename",
    # Sentiment + topics + intents (both raw JSON strings and parsed views)
    "sentiment",
    "sentiment_parsed",
    "topics",
    "topics_parsed",
    "intents",
    "intents_parsed",
    # Structured ML classifications
    "call_type",
    "sale_result",
    "product_family",
    "product_category_detail",
    "no_sale_reasons",
    "problems_detected",
    "escalation_actions",
    "delivery_tracking",
    "confidence_scores",
)


def redact_sensitive_call_details(payload: dict) -> dict:
    """Strip analysis fields when a call is flagged sensitive.

    Should be called by the route AFTER ``get_call_details`` and AFTER
    checking the viewer's capabilities — if the viewer holds
    ``support.calls.view_sensitive``, pass the payload through unchanged.

    Mutates and returns the same dict (in-place) for convenience. Sets
    ``redacted_for_sensitive=True`` so the template can show the
    "restricted" banner.
    """
    if not payload or not payload.get("is_sensitive"):
        return payload
    for k in _SENSITIVE_FIELDS:
        if k in payload:
            payload[k] = None
    payload["redacted_for_sensitive"] = True
    return payload


# ---------------------------------------------------------------------------
# Cross-platform overlap merge
# ---------------------------------------------------------------------------

def _merge_overlapping_legs(entries: list[dict]) -> list[dict]:
    """Merge same-phone legs whose time windows overlap, regardless of source.

    A single physical call can ring on CXone briefly (e.g. 9 seconds)
    before forwarding to a PBX line where it's actually answered for
    several minutes. CXone and PBX track these as wholly separate
    sessions with no shared identifier — the only signal they're the
    same call is that their time windows overlap.

    Walks ``entries`` in start-time order and clusters legs whose
    ``call_time`` is within 10 seconds of the running max end-time. The
    leg with the longest ``duration_seconds`` per cluster wins (the
    platform that actually had the conversation; the bouncing leg is
    typically very short). The 10s buffer absorbs minor clock skew
    between PBX webhooks and the cxone-poller.
    """
    if not entries:
        return entries
    from datetime import timedelta

    # Group by (phone-equivalent) — entries here are already for the
    # same phone, so we just sort and run the gap-and-island walk.
    sorted_entries = sorted(entries, key=lambda c: c.get("call_time") or 0)
    clusters: list[list[dict]] = []
    current: list[dict] = []
    running_max_end = None
    BUFFER = timedelta(seconds=10)

    for e in sorted_entries:
        start = e.get("call_time")
        end = e.get("_call_end") or start
        if not current:
            current = [e]
            running_max_end = end
            continue
        if start is None or running_max_end is None or start <= running_max_end + BUFFER:
            current.append(e)
            if end and end > running_max_end:
                running_max_end = end
        else:
            clusters.append(current)
            current = [e]
            running_max_end = end
    if current:
        clusters.append(current)

    out: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            out.append(cluster[0])
            continue
        # Pick the leg with the longest duration as primary
        primary = max(cluster, key=lambda c: c.get("duration_seconds") or 0)
        # Carry a hint that this was a multi-platform / multi-leg call
        primary = dict(primary)
        primary["leg_count"] = len(cluster)
        primary["is_transferred"] = True
        out.append(primary)
    # Newest first for the UI
    out.sort(key=lambda c: c.get("call_time") or 0, reverse=True)
    return out


def _row_to_dict(row) -> Optional[dict]:
    """Recursively convert a BigQuery Row (or None) to a plain dict.

    BigQuery returns nested STRUCTs as Row objects and arrays as lists.
    We flatten everything to dict/list so the Jinja template doesn't have
    to know about BigQuery types.
    """
    if row is None:
        return None
    if hasattr(row, "items"):
        return {k: _coerce(v) for k, v in row.items()}
    return _coerce(row)


def _coerce(v):
    if v is None:
        return None
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if hasattr(v, "items"):
        return {k: _coerce(x) for k, x in v.items()}
    # datetime/date/decimal etc. — let Flask's jsonify handle them, or stringify in template
    return v
