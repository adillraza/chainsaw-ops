"""Read the Neto shipping-config snapshot from BigQuery + usage overlays.

The canonical config lives in BigQuery ``neto_shipping.*`` (written by the
standalone scraper at chainsaw-functions/neto-shipping-scraper). This service
reads the latest snapshot for the NETO Shippings tab and enriches it with live
usage data so the visuals show not just *how* shipping is wired but *how much*
flows through each path.

Three overlays:
* product counts per ShippingCategory   (netocssv2.Products)
* order volume + $ per ShippingOption    (dataform.neto_orders, last 365d)
* actual carrier cost band per service   (startrack._all_invoices)

Everything is cached in-process (TTL) since the config changes rarely and the
tab is read far more often than the scraper runs.
"""
from __future__ import annotations

import logging
import time
from typing import Any

PROJECT = "chainsawspares-385722"
DATASET = "neto_shipping"
_TTL = 1800  # 30 min

log = logging.getLogger(__name__)

# module-level cache: key -> (expires_at, value)
_cache: dict[str, tuple[float, Any]] = {}


def _bq():
    from app.services.purchase_orders_service import purchase_orders_service
    return purchase_orders_service.client


def _cached(key: str, loader):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = loader()
    _cache[key] = (now + _TTL, value)
    return value


def bust_cache() -> None:
    """Clear the read cache — call after a fresh scrape so the tab repaints."""
    _cache.clear()


def _rows(sql: str) -> list[dict]:
    client = _bq()
    if client is None:
        raise RuntimeError("BigQuery client unavailable")
    return [dict(r) for r in client.query(sql).result()]


# ---------------------------------------------------------------------------
# Latest config snapshot (active-only by default)
# ---------------------------------------------------------------------------

def _latest_snapshot_id() -> str | None:
    rows = _rows(
        f"SELECT MAX(snapshot_id) AS sid FROM `{PROJECT}.{DATASET}.scrape_runs` "
        "WHERE status = 'ok'"
    )
    return rows[0]["sid"] if rows and rows[0]["sid"] else None


def get_snapshot(active_only: bool = True) -> dict[str, Any]:
    """Return the latest config snapshot: carriers, categories, options,
    services, mapping (+ snapshot metadata)."""
    def loader():
        sid = _latest_snapshot_id()
        if not sid:
            return {"snapshot_id": None, "carriers": [], "categories": [],
                    "options": [], "services": [], "mapping": [], "meta": {}}

        def tbl(name, active_clause=""):
            return _rows(
                f"SELECT * FROM `{PROJECT}.{DATASET}.{name}` "
                f"WHERE snapshot_id = '{sid}' {active_clause}"
            )

        carriers = tbl("carriers", "AND is_active" if active_only else "")
        categories = tbl("categories", "AND is_active" if active_only else "")
        options = tbl("options", "AND is_active" if active_only else "")
        services = tbl("services", "AND is_active" if active_only else "")
        mapping = tbl("mapping", "AND block_active" if active_only else "")
        meta = _rows(
            f"SELECT snapshot_id, scraped_at, source, duration_s, status, "
            f"n_carriers, n_categories, n_options, n_services, n_mapping "
            f"FROM `{PROJECT}.{DATASET}.scrape_runs` WHERE snapshot_id = '{sid}'"
        )
        return {
            "snapshot_id": sid,
            "carriers": carriers, "categories": categories, "options": options,
            "services": services, "mapping": mapping,
            "meta": meta[0] if meta else {},
        }

    return _cached(f"snapshot:{active_only}", loader)


# ---------------------------------------------------------------------------
# Usage overlays
# ---------------------------------------------------------------------------

def category_product_counts() -> dict[str, int]:
    """category_id -> count of active products assigned to it."""
    def loader():
        rows = _rows(
            "SELECT ShippingCategory AS cat, COUNT(*) AS n "
            f"FROM `{PROJECT}.netocssv2.Products` "
            "WHERE IsActive = 'True' AND ShippingCategory IS NOT NULL "
            "GROUP BY 1"
        )
        return {str(r["cat"]): int(r["n"]) for r in rows}
    return _cached("cat_counts", loader)


def option_order_stats(days: int = 365) -> dict[str, dict]:
    """ShippingOption -> {orders, total_shipping, avg_shipping} over last N days."""
    def loader():
        rows = _rows(
            "SELECT ShippingOption AS opt, COUNT(*) AS orders, "
            "ROUND(SUM(SAFE_CAST(ShippingTotal AS FLOAT64)), 2) AS total_shipping, "
            "ROUND(AVG(SAFE_CAST(ShippingTotal AS FLOAT64)), 2) AS avg_shipping "
            f"FROM `{PROJECT}.dataform.neto_orders` "
            f"WHERE DatePlaced >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL {int(days)} DAY) "
            "AND ShippingOption IS NOT NULL GROUP BY 1"
        )
        return {r["opt"]: {"orders": int(r["orders"]),
                           "total_shipping": r["total_shipping"] or 0.0,
                           "avg_shipping": r["avg_shipping"] or 0.0} for r in rows}
    return _cached(f"opt_stats:{days}", loader)


def service_cost_bands() -> dict[str, dict]:
    """Service name (= NETO_ShippingMethods) -> actual billed Startrack cost band."""
    def loader():
        rows = _rows(
            "SELECT NETO_ShippingMethods AS svc, COUNT(*) AS n, "
            "ROUND(MIN(SAFE_CAST(Total_Charge AS FLOAT64)), 2) AS min_c, "
            "ROUND(APPROX_QUANTILES(SAFE_CAST(Total_Charge AS FLOAT64), 2)[OFFSET(1)], 2) AS median_c, "
            "ROUND(MAX(SAFE_CAST(Total_Charge AS FLOAT64)), 2) AS max_c "
            f"FROM `{PROJECT}.startrack._all_invoices` "
            "WHERE Total_Charge IS NOT NULL AND NETO_ShippingMethods IS NOT NULL "
            "GROUP BY 1"
        )
        return {r["svc"]: {"n": int(r["n"]), "min": r["min_c"],
                           "median": r["median_c"], "max": r["max_c"]} for r in rows}
    return _cached("svc_cost_bands", loader)
