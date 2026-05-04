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
            "customers": [],         # list of customer_360 rows (one per matching username)
            "call_history": None,    # call_history_360 row, or None
            "call_behavior": None,   # call_behavior_360 row, or None
            "error": None,
        }
        if not phone:
            empty["error"] = "Empty / unparseable phone number"
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

        return empty

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

        # Group by session: pick latest event, also track earliest received_at
        # for the call's "ringing/talking for X" timer.
        latest_per_session: dict[str, "CallEvent"] = {}
        earliest_at: dict[str, datetime] = {}
        for e in events:
            sid = e.session_id
            if sid not in latest_per_session:
                latest_per_session[sid] = e  # rows came in DESC, first wins
            if sid not in earliest_at or e.received_at < earliest_at[sid]:
                earliest_at[sid] = e.received_at

        # Find a session whose latest event is NOT a Disconnected
        for sid, latest in latest_per_session.items():
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
        if row is None:
            return empty

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
