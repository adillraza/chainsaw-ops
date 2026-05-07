"""SQLite cache for the Dataform tables that feed the live-call Customer 360 card.

Each refresh truncates the five cache tables and bulk-loads them from BigQuery.
The card-load read path stays in :mod:`app.services.customer_360_service` and
falls back to BigQuery on cache miss — so a partially-populated cache never
breaks the UI, just degrades to slower BQ reads until the next refresh.

Run via the ``flask refresh-cache`` CLI command; the
``chainsaw-ops-refresh.timer`` fires it at ``:05`` and ``:35`` — a few minutes
after each ``customer360-hourly`` Dataform run completes.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Iterable

from flask import current_app

from app.extensions import db
from app.models.customer_cache import (
    CachedCallBehavior,
    CachedCallHistory,
    CachedCustomer360,
    CachedNetoProduct,
    CachedPhoneLookup,
)
from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"
DATASET = "dataform"
INSERT_BATCH = 500


def _row_to_dict(row) -> dict:
    """BigQuery Row → plain dict, recursing into RECORD/ARRAY values.

    Mirrors the helper in customer_360_service so cache payloads come out the
    exact shape the service has always handed to the templates.
    """
    if row is None:
        return {}
    out = {}
    for key, val in row.items():
        out[key] = _coerce(val)
    return out


def _coerce(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "items"):
        return _row_to_dict(v)
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _bulk_replace(model, rows: Iterable[dict], label: str) -> int:
    """Truncate + bulk-insert a cache table. Returns row count inserted."""
    sess = db.session
    sess.execute(db.delete(model))
    sess.commit()
    n = 0
    batch: list[dict] = []
    t0 = time.perf_counter()
    for r in rows:
        batch.append(r)
        if len(batch) >= INSERT_BATCH:
            sess.bulk_insert_mappings(model, batch)
            sess.commit()
            n += len(batch)
            batch.clear()
    if batch:
        sess.bulk_insert_mappings(model, batch)
        sess.commit()
        n += len(batch)
    print(f"  {label}: {n:,} rows in {(time.perf_counter()-t0):.1f}s")
    return n


# ---------------------------------------------------------------------------
# Per-table loaders — each yields (model, dict) tuples
# ---------------------------------------------------------------------------

def _load_phone_lookup(client) -> int:
    sql = f"SELECT phone, usernames, match_count, is_international FROM `{PROJECT}.{DATASET}.customer_phone_lookup`"

    def gen():
        for r in client.query(sql).result():
            yield {
                "phone":            r.phone,
                "usernames_json":   json.dumps(list(r.usernames or [])),
                "match_count":      r.match_count,
                "is_international": r.is_international,
                "cached_at":        datetime.utcnow(),
            }
    return _bulk_replace(CachedPhoneLookup, gen(), "phone_lookup")


def _load_customer_360(client) -> int:
    sql = f"SELECT * FROM `{PROJECT}.{DATASET}.customer_360`"

    def gen():
        # The Dataform model is *intended* as one-row-per-Username, but in
        # practice ~150 dupes leak through (likely a JOIN-fan in one of
        # the recent_order CTEs). Last-wins dedupe locally — flag for a
        # Dataform fix in BACKLOG.
        seen: set[str] = set()
        skipped = 0
        for r in client.query(sql).result():
            uname = r.Username
            if not uname:
                continue
            if uname in seen:
                skipped += 1
                continue
            seen.add(uname)
            d = _row_to_dict(r)
            yield {
                "Username":        uname,
                "email":           d.get("email"),
                "secondary_email": d.get("secondary_email"),
                "last_order_date": r.last_order_date,
                "last_rma_date":   r.last_rma_date,
                "payload_json":    json.dumps(d, default=str),
                "cached_at":       datetime.utcnow(),
            }
        if skipped:
            print(f"  customer_360 dedupe: skipped {skipped:,} duplicate Username rows")
    return _bulk_replace(CachedCustomer360, gen(), "customer_360")


def _load_call_history(client) -> int:
    sql = f"SELECT * FROM `{PROJECT}.{DATASET}.call_history_360`"

    def gen():
        for r in client.query(sql).result():
            d = _row_to_dict(r)
            yield {
                "phone":          d.get("phone"),
                "last_call_date": r.last_call_date,
                "payload_json":   json.dumps(d, default=str),
                "cached_at":      datetime.utcnow(),
            }
    return _bulk_replace(CachedCallHistory, gen(), "call_history")


def _load_call_behavior(client) -> int:
    sql = f"SELECT * FROM `{PROJECT}.{DATASET}.call_behavior_360`"

    def gen():
        for r in client.query(sql).result():
            d = _row_to_dict(r)
            yield {
                "phone":        d.get("phone"),
                "payload_json": json.dumps(d, default=str),
                "cached_at":    datetime.utcnow(),
            }
    return _bulk_replace(CachedCallBehavior, gen(), "call_behavior")


def _load_neto_product(client) -> int:
    sql = f"SELECT SKU AS sku, ID AS product_id, Name AS name FROM `{PROJECT}.{DATASET}.neto_product_list`"

    def gen():
        seen: set[str] = set()
        for r in client.query(sql).result():
            sku = (r.sku or "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            yield {
                "sku":        sku,
                "product_id": str(r.product_id) if r.product_id is not None else None,
                "name":       (r.name or "")[:500] or None,
                "cached_at":  datetime.utcnow(),
            }
    return _bulk_replace(CachedNetoProduct, gen(), "neto_product")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def cache_customer_360_data() -> tuple[bool, str]:
    """Refresh the Customer-360 cache tables from BigQuery.

    Phase 1 scope (Option A3): we cache the four "small" tables:
    ``customer_phone_lookup``, ``call_history_360``, ``call_behavior_360``,
    and ``neto_product_list``. These are the bulk of the per-card BQ
    latency cost. ``customer_360`` itself is intentionally *not* cached —
    it's 328k rows × ~1.8KB = ~600MB, which would push refresh time past
    10 minutes per run. Card load uses a single BQ round-trip for that
    table (see ``Customer360Service._fetch_customers``). Phase 2 will
    add it back via incremental sync.

    Designed to run from the ``flask refresh-cache`` CLI inside an app
    context. Returns ``(success, message)`` so the CLI can surface a
    clean error to the systemd journal.
    """
    app = current_app._get_current_object()
    with app.app_context():
        client = purchase_orders_service.client
        if client is None:
            return False, "BigQuery client not available"
        try:
            t0 = time.perf_counter()
            print("Refreshing Customer 360 cache from BigQuery...")
            total = 0
            total += _load_phone_lookup(client)
            total += _load_call_history(client)
            total += _load_call_behavior(client)
            total += _load_neto_product(client)
            secs = time.perf_counter() - t0
            return True, f"customer_360 cache refreshed: {total:,} rows in {secs:.1f}s"
        except Exception as exc:
            db.session.rollback()
            return False, f"customer_360 cache refresh failed: {exc}"
