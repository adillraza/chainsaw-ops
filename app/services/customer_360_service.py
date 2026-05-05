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
import re
from typing import Optional

from google.cloud import bigquery

from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"
DATASET = "dataform"


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
        """Bulk SKU → Neto product ID lookup against ``dataform.neto_product_list``."""
        if not skus or self.client is None:
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

        Resolves phone → username via ``customer_phone_lookup`` and picks
        the highest-lifetime-value customer when multiple records share
        the number (household / repeat-guest case).
        """
        phone = normalize_phone(raw_phone)
        if not phone or self.client is None:
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
        # Avoid the reserved word `lookup` (BQ keyword) in column aliases.
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
    # Per-call detail: AI summary + transcript + classifications + audio
    # ------------------------------------------------------------------

    def get_call_details(self, session_id: str) -> dict:
        """Pull the full bundle for one historical call (for the modal).

        Sources:
          * recording_fetch_status — transcript, summary, sentiment, topics,
            intents, gcs_uri (audio file)
          * call_classifications  — structured AI fields (call_type, sale_result,
            problems_detected, etc.)
          * callsrep_rep_contacts_completed_v2 — call metadata (start/duration/agent)

        ``session_id`` matches CXone calls (numeric contactId stored as STRING in
        our last_5_calls). PBX calls use a different session id format
        (``s-...``) and won't match — for those we return an empty payload and
        the modal renders a "no analysis available" state.
        """
        empty = {
            "session_id": session_id,
            "found": False,
            "recording_signed_url": None,
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

        # If nothing found in the analyzer output (recording_fetch_status),
        # fall back to the local call_event log — covers today's calls
        # (analyzer hasn't run yet) and any PBX session_ids that bypass
        # the analyzer pipeline.
        if row is None:
            return self._call_details_from_event_log(session_id)

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

        return d

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
            return {"session_id": session_id, "found": False}

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
