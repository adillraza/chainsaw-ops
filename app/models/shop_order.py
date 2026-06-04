"""Local SQLite cache of the two Shop-Order recommendation models.

Mirrors the BigQuery Dataform tables that drive the daily preview emails:
  * CachedShopOrderMsl   <- dataform.po_preview_lines       (MSL replica)
  * CachedShopOrderSmart <- dataform.rex_po_recommendation  (velocity/seasonal)

Rebuilt by ``app.services.shop_order_cache.cache_shop_order_data`` on the same
:05/:35 timer as the PO cache, so the Shop Order screen is always as fresh as
the half-hourly Dataform run without needing to fire the preview email.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CachedShopOrderMsl(db.Model):
    """One row per po_preview_lines line (ORDER + NEEDS_ADJUSTMENT)."""
    id = db.Column(db.Integer, primary_key=True)
    bucket = db.Column(db.Integer, index=True)
    line_type = db.Column(db.String(20), index=True)  # ORDER | NEEDS_ADJUSTMENT
    manufacturer_sku = db.Column(db.String(100), index=True)
    short_description = db.Column(db.String(500))
    product_type_name = db.Column(db.String(150))
    msl = db.Column(db.Integer)
    available = db.Column(db.Integer)
    on_order = db.Column(db.Integer)
    re_order_qty = db.Column(db.Integer)
    sold_last_14_days = db.Column(db.Integer)
    sold_next_14_days_last_year = db.Column(db.Integer)
    seasonal_bump = db.Column(db.Integer)
    raw_qty = db.Column(db.Integer)
    carton_quantity = db.Column(db.Integer)
    proposed_qty = db.Column(db.Integer)
    adjustment_qty = db.Column(db.Integer)
    supplier_buy_ex = db.Column(db.Float)
    estimated_line_value = db.Column(db.Float)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class CachedSeasonalityIndex(db.Model):
    """One row per (product_type x month) from dataform.rex_seasonality_index."""
    id = db.Column(db.Integer, primary_key=True)
    product_type = db.Column(db.String(150), index=True)  # '_STORE_' = baseline
    month = db.Column(db.Integer, index=True)             # 1-12
    seasonal_index = db.Column(db.Float)                  # 1.0 = average month
    sample_units = db.Column(db.Integer)
    years_covered = db.Column(db.Integer)
    confidence = db.Column(db.String(12))                 # HIGH | MEDIUM | LOW | BASELINE
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class CachedShopOrderSmart(db.Model):
    """One row per rex_po_recommendation line."""
    id = db.Column(db.Integer, primary_key=True)
    bucket = db.Column(db.Integer, index=True)
    urgency = db.Column(db.String(20), index=True)   # CRITICAL | HIGH | MEDIUM | LOW
    category = db.Column(db.String(20))              # DEAD | SPIKY | COVERED | URGENT | ORDER
    manufacturer_sku = db.Column(db.String(100), index=True)
    short_description = db.Column(db.String(500))
    product_type_name = db.Column(db.String(150))
    available = db.Column(db.Integer)
    msl = db.Column(db.Integer)
    on_order = db.Column(db.Integer)
    carton_quantity = db.Column(db.Integer)
    s14 = db.Column(db.Integer)
    s30 = db.Column(db.Integer)
    lyr30 = db.Column(db.Integer)
    yr2_30 = db.Column(db.Integer)
    daily_velocity = db.Column(db.Float)
    seasonal_factor = db.Column(db.Float)
    forecast_30d = db.Column(db.Integer)
    coverage_days = db.Column(db.Integer)
    lead_days = db.Column(db.Integer)
    recommended_qty = db.Column(db.Integer)
    supplier_buy_ex = db.Column(db.Float)
    estimated_line_value = db.Column(db.Float)
    reasoning = db.Column(db.Text)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
