"""Local SQLite cache of the Dataform tables that drive the live-call
Customer 360 card.

Each cached table mirrors a Dataform model (``customer_360``,
``customer_phone_lookup``, ``call_history_360``, ``call_behavior_360``,
``neto_product_list``). The full row is stored as a JSON blob in
``payload_json``; a small handful of indexed columns are pulled out so the
service layer can do its lookups without parsing every row.

The card-load read path is:

    phone -> CachedPhoneLookup -> [usernames]
    phone -> CachedCallHistory.payload_json
    phone -> CachedCallBehavior.payload_json
    [usernames] -> CachedCustomer360.payload_json
    [skus]      -> CachedNetoProduct

Refreshed by ``app.services.cache.cache_customer_360_data`` on the
``chainsaw-ops-refresh.timer`` schedule (a few minutes after each
``customer360-hourly`` Dataform run completes).
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CachedCustomer360(db.Model):
    __tablename__ = "cached_customer_360"

    Username        = db.Column(db.String(150), primary_key=True)
    email           = db.Column(db.String(255), index=True)
    secondary_email = db.Column(db.String(255), index=True)
    last_order_date = db.Column(db.Date)
    last_rma_date   = db.Column(db.Date)
    payload_json    = db.Column(db.Text, nullable=False)
    cached_at       = db.Column(db.DateTime, default=datetime.utcnow)


class CachedPhoneLookup(db.Model):
    __tablename__ = "cached_phone_lookup"

    phone            = db.Column(db.String(50), primary_key=True)
    usernames_json   = db.Column(db.Text, nullable=False)  # JSON array
    match_count      = db.Column(db.Integer)
    is_international = db.Column(db.Boolean)
    cached_at        = db.Column(db.DateTime, default=datetime.utcnow)


class CachedCallHistory(db.Model):
    __tablename__ = "cached_call_history"

    phone          = db.Column(db.String(50), primary_key=True)
    last_call_date = db.Column(db.Date)
    payload_json   = db.Column(db.Text, nullable=False)
    cached_at      = db.Column(db.DateTime, default=datetime.utcnow)


class CachedCallBehavior(db.Model):
    __tablename__ = "cached_call_behavior"

    phone        = db.Column(db.String(50), primary_key=True)
    payload_json = db.Column(db.Text, nullable=False)
    cached_at    = db.Column(db.DateTime, default=datetime.utcnow)


class CachedNetoProduct(db.Model):
    __tablename__ = "cached_neto_product"

    sku        = db.Column(db.String(100), primary_key=True)
    product_id = db.Column(db.String(50))
    name       = db.Column(db.String(500))
    cached_at  = db.Column(db.DateTime, default=datetime.utcnow)
