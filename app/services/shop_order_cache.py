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
    CachedWeatherAlert,
    CachedWeatherCurrent,
    CachedWeatherForecast,
)
from app.services.purchase_orders_service import purchase_orders_service
from app.utils.dates import safe_parse_date

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


# weather tables only exist once `flask fetch-weather` has run at least once.
_WX_CURRENT_SQL = (
    "SELECT * FROM `{project}.operations.weather_current` "
    "ORDER BY fetched_at DESC LIMIT 1"
)
_WX_FORECAST_SQL = (
    "SELECT * FROM `{project}.operations.weather_forecast` "
    "WHERE fetched_at = (SELECT MAX(fetched_at) FROM `{project}.operations.weather_forecast`) "
    "ORDER BY day_offset"
)
_WX_ALERTS_SQL = """
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY fetched_at DESC) rn
  FROM `{project}.operations.weather_alerts`
  WHERE fetched_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
SELECT * EXCEPT(rn) FROM ranked WHERE rn = 1
ORDER BY distance_km IS NULL, distance_km ASC
"""


def cache_weather_data():
    """Mirror the latest weather snapshot + 30-day alerts into SQLite.

    Independent of cache_shop_order_data so a weather glitch never blocks the
    MSL/Smart/Seasonality tabs.  Returns (success, message).
    """
    client = getattr(purchase_orders_service, "client", None)
    project = getattr(purchase_orders_service, "project_id", None)
    if client is None or not project:
        return False, "BigQuery client not initialised"
    try:
        cur = list(client.query(_WX_CURRENT_SQL.format(project=project)).result())
        fc = list(client.query(_WX_FORECAST_SQL.format(project=project)).result())
        al = list(client.query(_WX_ALERTS_SQL.format(project=project)).result())

        now = datetime.utcnow()
        CachedWeatherCurrent.query.delete()
        CachedWeatherForecast.query.delete()
        CachedWeatherAlert.query.delete()
        db.session.commit()

        for r in cur:
            db.session.add(CachedWeatherCurrent(
                fetched_at=safe_parse_date(r.get("fetched_at")),
                temp_c=_f(r.get("temp_c")), apparent_c=_f(r.get("apparent_c")),
                precip_mm=_f(r.get("precip_mm")), wind_kmh=_f(r.get("wind_kmh")),
                weather_label=r.get("weather_label"), is_day=bool(r.get("is_day")),
                cached_at=now,
            ))
        for r in fc:
            db.session.add(CachedWeatherForecast(
                forecast_date=str(r.get("forecast_date")) if r.get("forecast_date") is not None else None,
                day_offset=_i(r.get("day_offset")),
                temp_min=_f(r.get("temp_min")), temp_max=_f(r.get("temp_max")),
                precip_mm=_f(r.get("precip_mm")), precip_prob_max=_i(r.get("precip_prob_max")),
                wind_max_kmh=_f(r.get("wind_max_kmh")), weather_label=r.get("weather_label"),
                cached_at=now,
            ))
        for r in al:
            db.session.add(CachedWeatherAlert(
                source_id=r.get("source_id"), feed_type=r.get("feed_type"),
                category1=r.get("category1"), category2=r.get("category2"),
                status=r.get("status"), headline=r.get("headline"), action=r.get("action"),
                location=r.get("location"), alert_text=r.get("text"),
                created=str(r.get("created")) if r.get("created") is not None else None,
                updated=str(r.get("updated")) if r.get("updated") is not None else None,
                distance_km=_f(r.get("distance_km")), url=r.get("url"),
                fetched_at=safe_parse_date(r.get("fetched_at")),
                cached_at=now,
            ))
        db.session.commit()
        return True, f"cached weather + {len(fc)}d forecast + {len(al)} alerts"
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return False, f"weather cache failed: {exc}"
