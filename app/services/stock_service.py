"""REX Stock explorer — full stock picture for a single SKU.

On-demand (search-driven), so this queries BigQuery live rather than caching:
  * current inventory snapshot   (dataform.rex_ballarat_inventory)
  * full movement history + a reconstructed running stock-on-hand
                                 (dataform.rex_inventory_movement_logs)
  * lifetime summary             (ins / outs / adjustments / stock takes)

The running balance is the cumulative sum of quantity_soh_delta ordered by
time — i.e. physical stock-on-hand after each movement. It won't always equal
REX "available" (available = on-hand minus allocated/picked), which is exactly
the kind of gap this screen is meant to make visible.
"""
from __future__ import annotations

from google.cloud import bigquery

from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"

_SNAPSHOT_SQL = f"""
SELECT product_id, manufacturer_sku, supplier_sku, short_description,
       product_type_name, supplier_name,
       CAST(available AS INT64)  AS available,
       CAST(on_order AS INT64)   AS on_order,
       CAST(msl AS INT64)        AS msl,
       CAST(allocated AS INT64)  AS allocated,
       CAST(transit_in AS INT64) AS transit_in,
       CAST(received AS INT64)   AS received,
       CAST(requested AS INT64)  AS requested,
       CAST(faulty AS INT64)     AS faulty,
       supplier_buy_ex
FROM `{PROJECT}.dataform.rex_ballarat_inventory`
WHERE LOWER(TRIM(manufacturer_sku)) = LOWER(TRIM(@sku))
   OR LOWER(TRIM(supplier_sku))     = LOWER(TRIM(@sku))
LIMIT 1
"""

_MOVEMENTS_SQL = f"""
SELECT
  created_on_aest, movement_type, origin, created_by, comment, change_labels,
  CAST(quantity_soh_delta AS INT64)      AS soh_delta,
  CAST(quantity_onorder_delta AS INT64)  AS onorder_delta,
  CAST(quantity_received_delta AS INT64) AS received_delta,
  CAST(quantity_allocated_delta AS INT64) AS allocated_delta,
  CAST(SUM(quantity_soh_delta) OVER (ORDER BY created_on_aest, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS INT64) AS running_soh
FROM `{PROJECT}.dataform.rex_inventory_movement_logs`
WHERE product_id = @pid
ORDER BY created_on_aest DESC, id DESC
LIMIT 500
"""

_SUMMARY_SQL = f"""
SELECT
  COUNT(*) AS events,
  MIN(created_on_aest) AS first_movement,
  MAX(created_on_aest) AS last_movement,
  ROUND(SUM(IF(quantity_soh_delta > 0, quantity_soh_delta, 0))) AS total_in,
  ROUND(SUM(IF(quantity_soh_delta < 0, quantity_soh_delta, 0))) AS total_out,
  COUNTIF(movement_type = 'Manual Adjustment') AS manual_adjustments,
  ROUND(SUM(IF(movement_type = 'Manual Adjustment', quantity_soh_delta, 0))) AS net_manual_adjustment,
  COUNTIF(movement_type = 'Stock Take') AS stock_takes,
  ROUND(SUM(IF(movement_type = 'Stock Take', quantity_soh_delta, 0))) AS net_stock_take,
  MAX(IF(movement_type = 'Manual Adjustment', created_on_aest, NULL)) AS last_adjustment
FROM `{PROJECT}.dataform.rex_inventory_movement_logs`
WHERE product_id = @pid
"""


def _rows(client, sql, params):
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(r) for r in job.result()]


def get_stock_picture(sku: str):
    """Return {snapshot, movements, summary} for a SKU, or None if not found."""
    client = getattr(purchase_orders_service, "client", None)
    if client is None or not sku:
        return None

    snap = _rows(client, _SNAPSHOT_SQL, [bigquery.ScalarQueryParameter("sku", "STRING", sku.strip())])
    if not snap:
        return None
    snapshot = snap[0]
    if snapshot.get("product_id") is None:
        return None
    pid = int(snapshot["product_id"])  # BQ may hand back NUMERIC/Decimal
    snapshot["product_id"] = pid

    movements = _rows(client, _MOVEMENTS_SQL, [bigquery.ScalarQueryParameter("pid", "INT64", pid)])
    summary = _rows(client, _SUMMARY_SQL, [bigquery.ScalarQueryParameter("pid", "INT64", pid)])
    summary = summary[0] if summary else {}

    # Reconstructed on-hand from the full log (most recent row's running_soh).
    # movements is DESC, so the first row carries the latest running total.
    reconstructed_soh = movements[0]["running_soh"] if movements else None
    summary["reconstructed_soh"] = reconstructed_soh
    summary["available"] = snapshot.get("available")
    # gap between physical-on-hand reconstruction and REX available — the tell.
    if reconstructed_soh is not None and snapshot.get("available") is not None:
        summary["soh_vs_available_gap"] = reconstructed_soh - snapshot["available"]

    return {"snapshot": snapshot, "movements": movements, "summary": summary}
