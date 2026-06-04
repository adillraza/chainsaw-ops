"""Rebuild the Shop-Order SQLite cache from BigQuery.

Pulls the two recommendation models that drive the daily preview emails into
local cache tables so the Ops "Shop Order" screen renders instantly:

    dataform.po_preview_lines       -> CachedShopOrderMsl
    dataform.rex_po_recommendation  -> CachedShopOrderSmart

Reuses the BigQuery client already initialised by ``purchase_orders_service``.
Wired into ``flask refresh-cache`` (app/cli.py) so it runs on the same
:05/:35 timer as the PO cache.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.models.shop_order import (
    CachedSeasonalityIndex,
    CachedShopOrderMsl,
    CachedShopOrderSmart,
)
from app.services.purchase_orders_service import purchase_orders_service

# SELECT * (not an explicit column list) on purpose: the Dataform production
# release sometimes lags main by up to a day, so a freshly-added column (e.g.
# seasonal_factor) may be briefly absent from the live table. With * + .get()
# below, a missing column just maps to None instead of crashing the refresh.
_MSL_SQL = "SELECT * FROM `{project}.dataform.po_preview_lines`"
_SMART_SQL = "SELECT * FROM `{project}.dataform.rex_po_recommendation`"
_SEASON_SQL = "SELECT * FROM `{project}.dataform.rex_seasonality_index`"


def _i(v):
    """Best-effort int (BQ NUMERIC/FLOAT/None -> int/None)."""
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cache_shop_order_data():
    """Rebuild both Shop-Order cache tables. Returns (success, message)."""
    client = getattr(purchase_orders_service, "client", None)
    project = getattr(purchase_orders_service, "project_id", None)
    if client is None or not project:
        return False, "BigQuery client not initialised"

    try:
        msl_rows = list(client.query(_MSL_SQL.format(project=project)).result())
        smart_rows = list(client.query(_SMART_SQL.format(project=project)).result())
        season_rows = list(client.query(_SEASON_SQL.format(project=project)).result())

        now = datetime.utcnow()

        # Full rebuild — these tables are small (low hundreds of rows each).
        CachedShopOrderMsl.query.delete()
        CachedShopOrderSmart.query.delete()
        CachedSeasonalityIndex.query.delete()
        db.session.commit()

        for r in msl_rows:
            db.session.add(CachedShopOrderMsl(
                bucket=_i(r.get("bucket")),
                line_type=r.get("line_type"),
                manufacturer_sku=r.get("manufacturer_sku"),
                short_description=r.get("short_description"),
                product_type_name=r.get("product_type_name"),
                msl=_i(r.get("msl")),
                available=_i(r.get("available")),
                on_order=_i(r.get("on_order")),
                re_order_qty=_i(r.get("re_order_qty")),
                sold_last_14_days=_i(r.get("sold_last_14_days")),
                sold_next_14_days_last_year=_i(r.get("sold_next_14_days_last_year")),
                seasonal_bump=_i(r.get("seasonal_bump")),
                raw_qty=_i(r.get("raw_qty")),
                carton_quantity=_i(r.get("carton_quantity")),
                proposed_qty=_i(r.get("proposed_qty")),
                adjustment_qty=_i(r.get("adjustment_qty")),
                supplier_buy_ex=_f(r.get("supplier_buy_ex")),
                estimated_line_value=_f(r.get("estimated_line_value")),
                cached_at=now,
            ))

        for r in smart_rows:
            db.session.add(CachedShopOrderSmart(
                bucket=_i(r.get("bucket")),
                urgency=r.get("urgency"),
                category=r.get("category"),
                manufacturer_sku=r.get("manufacturer_sku"),
                short_description=r.get("short_description"),
                product_type_name=r.get("product_type_name"),
                available=_i(r.get("available")),
                msl=_i(r.get("msl")),
                on_order=_i(r.get("on_order")),
                carton_quantity=_i(r.get("carton_quantity")),
                s14=_i(r.get("s14")),
                s30=_i(r.get("s30")),
                lyr30=_i(r.get("lyr30")),
                yr2_30=_i(r.get("yr2_30")),
                daily_velocity=_f(r.get("daily_velocity")),
                seasonal_factor=_f(r.get("seasonal_factor")),
                forecast_30d=_i(r.get("forecast_30d")),
                coverage_days=_i(r.get("coverage_days")),
                lead_days=_i(r.get("lead_days")),
                recommended_qty=_i(r.get("recommended_qty")),
                supplier_buy_ex=_f(r.get("supplier_buy_ex")),
                estimated_line_value=_f(r.get("estimated_line_value")),
                reasoning=r.get("reasoning"),
                cached_at=now,
            ))

        for r in season_rows:
            db.session.add(CachedSeasonalityIndex(
                product_type=r.get("product_type"),
                month=_i(r.get("month")),
                seasonal_index=_f(r.get("seasonal_index")),
                sample_units=_i(r.get("sample_units")),
                years_covered=_i(r.get("years_covered")),
                confidence=r.get("confidence"),
                cached_at=now,
            ))

        db.session.commit()
        return True, (f"cached {len(msl_rows)} MSL + {len(smart_rows)} smart "
                      f"+ {len(season_rows)} seasonality rows")
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return False, f"shop-order cache failed: {exc}"
