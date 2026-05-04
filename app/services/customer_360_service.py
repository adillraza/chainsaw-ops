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

        return empty

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
