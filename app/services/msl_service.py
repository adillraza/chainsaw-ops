"""BigQuery helpers for the MSL change approval workflow.

Reads from ``dataform.rex_ballarat_msl_changes`` (Dataform source view) and
writes to ``operations.msl_change_decisions`` (created via
``docs/bq_msl_change_decisions.sql``).

Natural key for an MSL change row: ``(manufacturer_sku, product_modified_on)``.
The pending query LEFT JOINs the decisions table on this key and filters out
rows that have been decided -- so a change that reappears later (new
``product_modified_on``) automatically re-enters the queue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from google.cloud import bigquery

from app.services.purchase_orders_service import purchase_orders_service


PROJECT_ID = "chainsawspares-385722"
SOURCE_VIEW = f"`{PROJECT_ID}.dataform.rex_ballarat_msl_changes`"
DECISIONS_TABLE = f"`{PROJECT_ID}.operations.msl_change_decisions`"


@dataclass
class MSLChange:
    """One pending MSL change row (still awaiting a decision)."""

    manufacturer_sku: str
    short_description: str | None
    supplier_code: str | None
    available: int | None
    carton_quantity: int | None
    new_msl: int | None
    previous_msl: int | None
    product_modified_on: datetime | None

    @classmethod
    def from_row(cls, row) -> "MSLChange":
        return cls(
            manufacturer_sku=row["manufacturer_sku"],
            short_description=row.get("short_description"),
            supplier_code=row.get("supplier_code"),
            available=row.get("available"),
            carton_quantity=row.get("carton_quantity"),
            new_msl=row.get("new_msl"),
            previous_msl=row.get("previous_msl"),
            product_modified_on=row.get("product_modified_on"),
        )

    @property
    def row_key(self) -> str:
        """Unique token the template uses for checkbox values."""
        ts = (
            self.product_modified_on.isoformat()
            if isinstance(self.product_modified_on, datetime)
            else str(self.product_modified_on)
        )
        return f"{self.manufacturer_sku}|{ts}"


@dataclass
class MSLDecision:
    """One audit-history row (already decided)."""

    manufacturer_sku: str
    short_description: str | None
    supplier_code: str | None
    previous_msl: int | None
    new_msl: int | None
    product_modified_on: datetime | None
    decision: str
    decided_by: str
    decided_at: datetime | None
    comment: str | None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_pending_msl_changes() -> tuple[list[MSLChange], str | None]:
    """Return rows from the source view that have no decision yet."""
    client = purchase_orders_service.client
    if client is None:
        return [], "BigQuery client not initialized"

    # The source view's ``product_modified_on`` is DATETIME while the
    # decisions table stores it as TIMESTAMP (matches our other operations.*
    # tables). Explicit cast in the join keeps both representations correct
    # without requiring a schema change.
    query = f"""
    WITH decided AS (
      SELECT manufacturer_sku, product_modified_on
      FROM {DECISIONS_TABLE}
      GROUP BY manufacturer_sku, product_modified_on
    )
    SELECT
      m.manufacturer_sku,
      m.short_description,
      m.supplier_code,
      m.available,
      m.carton_quantity,
      m.latest_msl  AS new_msl,
      m.previous_msl,
      m.product_modified_on
    FROM {SOURCE_VIEW} AS m
    LEFT JOIN decided AS d
      ON m.manufacturer_sku = d.manufacturer_sku
     AND TIMESTAMP(m.product_modified_on) = d.product_modified_on
    WHERE d.manufacturer_sku IS NULL
    ORDER BY m.product_modified_on DESC
    """
    try:
        rows = list(client.query(query).result())
        return [MSLChange.from_row(r) for r in rows], None
    except Exception as exc:  # pragma: no cover - surfaced in UI
        return [], str(exc)


def get_recent_decisions(days: int = 30, limit: int = 200) -> tuple[list[MSLDecision], str | None]:
    """Return decided rows within the last ``days``, newest first, capped at ``limit``."""
    client = purchase_orders_service.client
    if client is None:
        return [], "BigQuery client not initialized"

    query = f"""
    SELECT
      manufacturer_sku,
      short_description,
      supplier_code,
      previous_msl,
      new_msl,
      product_modified_on,
      decision,
      decided_by,
      decided_at,
      comment
    FROM {DECISIONS_TABLE}
    WHERE decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
    ORDER BY decided_at DESC
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("days", "INT64", days),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    try:
        rows = list(client.query(query, job_config=job_config).result())
    except Exception as exc:  # pragma: no cover
        return [], str(exc)

    return [
        MSLDecision(
            manufacturer_sku=r["manufacturer_sku"],
            short_description=r.get("short_description"),
            supplier_code=r.get("supplier_code"),
            previous_msl=r.get("previous_msl"),
            new_msl=r.get("new_msl"),
            product_modified_on=r.get("product_modified_on"),
            decision=r["decision"],
            decided_by=r["decided_by"],
            decided_at=r.get("decided_at"),
            comment=r.get("comment"),
        )
        for r in rows
    ], None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _lookup_pending_snapshot(keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], MSLChange]:
    """Re-fetch source-view rows for the given keys so each decision row
    captures an accurate snapshot of ``previous_msl`` / ``new_msl`` / etc.

    Keys are ``(manufacturer_sku, product_modified_on_iso)`` tuples, matching
    the checkbox values POSTed from the form. We query by SKU list (simple,
    indexable) and filter to the exact pairs in Python -- much cheaper than
    a struct-array ``IN UNNEST`` for the handful of rows a single bulk
    approve touches.
    """
    client = purchase_orders_service.client
    if client is None:
        return {}
    keys = list(keys)
    if not keys:
        return {}

    skus = sorted({sku for sku, _ in keys})
    wanted: set[tuple[str, str]] = set(keys)

    query = f"""
    SELECT
      manufacturer_sku,
      short_description,
      supplier_code,
      latest_msl  AS new_msl,
      previous_msl,
      product_modified_on
    FROM {SOURCE_VIEW}
    WHERE manufacturer_sku IN UNNEST(@skus)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("skus", "STRING", skus)]
    )
    try:
        rows = list(client.query(query, job_config=job_config).result())
    except Exception:
        return {}

    snap: dict[tuple[str, str], MSLChange] = {}
    for r in rows:
        pmo = r.get("product_modified_on")
        ts_iso = pmo.isoformat() if isinstance(pmo, datetime) else str(pmo)
        if (r["manufacturer_sku"], ts_iso) not in wanted:
            continue
        snap[(r["manufacturer_sku"], ts_iso)] = MSLChange(
            manufacturer_sku=r["manufacturer_sku"],
            short_description=r.get("short_description"),
            supplier_code=r.get("supplier_code"),
            available=None,
            carton_quantity=None,
            new_msl=r.get("new_msl"),
            previous_msl=r.get("previous_msl"),
            product_modified_on=pmo,
        )
    return snap


def record_decisions(
    keys: list[tuple[str, str]],
    decision: str,
    decided_by: str,
    comment: str | None = None,
) -> tuple[int, list[str]]:
    """Insert one ``msl_change_decisions`` row per ``(sku, ts)`` key.

    Returns ``(inserted_count, errors)`` where ``errors`` is a human-readable
    list suitable for flashing. We snapshot the view BEFORE inserting so the
    audit log is immutable even if the source row later changes.
    """
    assert decision in {"approved", "declined"}, decision

    client = purchase_orders_service.client
    if client is None:
        return 0, ["BigQuery client not initialized"]

    snap = _lookup_pending_snapshot(keys)
    if not snap:
        return 0, ["Could not resolve any of the selected rows against the source view."]

    def _as_int(v):
        # BigQuery returns NUMERIC columns as Decimal, which json.dumps can't
        # serialize. MSL values are integer counts so the coercion is lossless.
        return int(v) if v is not None else None

    now = datetime.utcnow()
    rows_to_insert: list[dict] = []
    missing_keys: list[str] = []
    for sku, ts in keys:
        change = snap.get((sku, ts))
        if change is None:
            missing_keys.append(f"{sku} @ {ts}")
            continue
        pmo = (
            change.product_modified_on.isoformat()
            if isinstance(change.product_modified_on, datetime)
            else change.product_modified_on
        )
        rows_to_insert.append({
            "decision_id": uuid.uuid4().hex,
            "manufacturer_sku": change.manufacturer_sku,
            "product_modified_on": pmo,
            "previous_msl": _as_int(change.previous_msl),
            "new_msl": _as_int(change.new_msl),
            "short_description": change.short_description,
            "supplier_code": change.supplier_code,
            "decision": decision,
            "decided_by": decided_by,
            "decided_at": now.isoformat(),
            "comment": comment or None,
        })

    errors: list[str] = []
    if missing_keys:
        errors.append(
            f"{len(missing_keys)} row(s) skipped (no longer in source view): "
            + ", ".join(missing_keys[:5])
            + ("…" if len(missing_keys) > 5 else "")
        )

    if not rows_to_insert:
        return 0, errors

    table_id = f"{PROJECT_ID}.operations.msl_change_decisions"
    try:
        insert_errors = client.insert_rows_json(table_id, rows_to_insert)
        if insert_errors:
            errors.append(f"BigQuery insert errors: {insert_errors}")
            return 0, errors
    except Exception as exc:
        errors.append(str(exc))
        return 0, errors

    return len(rows_to_insert), errors
