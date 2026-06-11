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


class CachedRelatedAccounts(db.Model):
    """Mirror of ``dataform.customer_related_accounts`` — per-Username list
    of OTHER usernames sharing an identity signal (same primary email,
    same secondary email, primary↔secondary cross-match, or same billing
    address). ``related_json`` is the JSON-encoded array of
    ``{related_username, match_type, match_value}`` structs. Phone-based
    matching is NOT in here — that stays in CachedPhoneLookup."""
    __tablename__ = "cached_related_accounts"

    Username      = db.Column(db.String(150), primary_key=True)
    related_json  = db.Column(db.Text, nullable=False)
    related_count = db.Column(db.Integer)
    cached_at     = db.Column(db.DateTime, default=datetime.utcnow)


class CacheWatermark(db.Model):
    """Per-table sync watermarks for incremental cache refresh.

    Each row tracks the most-recent ``last_modified_at`` value loaded into
    one cache table. The customer_360 incremental loader uses this to
    pull only rows changed since the previous run.
    """
    __tablename__ = "cache_watermark"

    cache_name      = db.Column(db.String(50), primary_key=True)
    last_synced_at  = db.Column(db.DateTime, nullable=False)
    rows_last_run   = db.Column(db.Integer)  # count from the most recent sync
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)


class CachedEmailMessage(db.Model):
    """Local mirror of ``email_archive.messages`` for the Customer 360
    Email History panel — lookup by from/to address."""
    __tablename__ = "cached_email_message"

    message_id         = db.Column(db.String(255), primary_key=True)
    conversation_id    = db.Column(db.String(255), index=True)
    from_address       = db.Column(db.String(255), index=True)
    from_name          = db.Column(db.String(255))
    subject            = db.Column(db.Text)
    received_at        = db.Column(db.DateTime, index=True)
    direction          = db.Column(db.String(10))   # inbound / outbound
    is_automated       = db.Column(db.Boolean)
    has_attachments    = db.Column(db.Boolean)
    body_preview       = db.Column(db.Text)         # truncated to 200 chars
    parent_folder_name = db.Column(db.String(255))
    web_link           = db.Column(db.Text)
    cached_at          = db.Column(db.DateTime, default=datetime.utcnow)


class CachedEmailRecipient(db.Model):
    """Recipient rows (to/cc/bcc) flattened so panel queries can match
    on any address with a single indexed lookup."""
    __tablename__ = "cached_email_recipient"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    message_id = db.Column(db.String(255), index=True, nullable=False)
    address    = db.Column(db.String(255), index=True, nullable=False)
