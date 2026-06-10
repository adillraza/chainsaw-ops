"""Read/write the staff-validated SKU capacity master.

`operations.sku_capacity` is **append-only**: every staff submission inserts a
new row; the *latest* row per SKU wins (audit trail preserved). The Final List
Dataform model (`rex_po_final`) reads the same latest-per-SKU view to cap how
much it will order.

Reuses the BigQuery client already initialised by ``purchase_orders_service``
(service account ``airbyte@…``, which has write access to the ``operations``
dataset — verified 2026-06-10).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.purchase_orders_service import purchase_orders_service

_TABLE = "{project}.operations.sku_capacity"

_LATEST_SQL = """
SELECT sku, capacity, has_space, total_qty_at, validated_by, validated_at
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY sku ORDER BY validated_at DESC) AS rn
  FROM `{project}.operations.sku_capacity`
)
WHERE rn = 1
"""


def _client_project():
    client = getattr(purchase_orders_service, "client", None)
    project = getattr(purchase_orders_service, "project_id", None)
    return client, project


def get_capacity_map() -> dict:
    """Return ``{sku: {capacity, has_space, total_qty_at, validated_by, validated_at}}``.

    The latest validation per SKU. Returns an empty dict on any error (so the
    page still renders — every SKU just shows as "needs validation").
    """
    client, project = _client_project()
    if not client or not project:
        return {}
    try:
        rows = client.query(_LATEST_SQL.format(project=project)).result()
        return {
            r["sku"]: {
                "capacity": r["capacity"],
                "has_space": r["has_space"],
                "total_qty_at": r["total_qty_at"],
                "validated_by": r["validated_by"],
                "validated_at": r["validated_at"],
            }
            for r in rows
        }
    except Exception:
        return {}


def insert_capacity(*, sku, capacity, has_space, available, on_order, proposed,
                    bucket, short_description, validated_by):
    """Append one validation row. Returns ``(ok: bool, message: str)``."""
    client, project = _client_project()
    if not client or not project:
        return False, "BigQuery client unavailable"

    available = int(available or 0)
    on_order = int(on_order or 0)
    proposed = int(proposed or 0)
    row = {
        "sku": sku,
        "capacity": int(capacity),
        "has_space": bool(has_space),
        "available_at": available,
        "on_order_at": on_order,
        "proposed_at": proposed,
        "total_qty_at": available + on_order + proposed,
        "bucket": int(bucket) if bucket not in (None, "") else None,
        "short_description": short_description,
        "validated_by": validated_by,
        # epoch seconds — unambiguous TIMESTAMP for streaming insert
        "validated_at": datetime.now(timezone.utc).timestamp(),
    }
    try:
        errors = client.insert_rows_json(_TABLE.format(project=project), [row])
    except Exception as exc:  # pragma: no cover - network/credential errors
        return False, str(exc)
    if errors:
        return False, str(errors)
    return True, "ok"
