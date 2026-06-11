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
    CacheWatermark,
    CachedCallBehavior,
    CachedCallHistory,
    CachedCustomer360,
    CachedNetoProduct,
    CachedPhoneLookup,
    CachedRelatedAccounts,
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
    # BigQuery NUMERIC arrives as decimal.Decimal — must coerce to float
    # so json.dumps(default=str) doesn't turn it into a string. The
    # template uses ``'%.0f' % primary.lifetime_value`` which only
    # works on real numbers.
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
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


def _bulk_upsert(model, rows: Iterable[dict], pk_col: str, label: str) -> int:
    """Insert-or-replace rows by primary key. Used by incremental loaders.

    SQLite-only — uses ``INSERT OR REPLACE`` semantics via raw SQL since
    SQLAlchemy's ORM upsert is dialect-specific. Each batch commits in
    one tx so partial failures don't leave the cache half-merged.
    """
    sess = db.session
    n = 0
    batch: list[dict] = []
    t0 = time.perf_counter()

    table = model.__table__

    def flush(batch):
        if not batch:
            return 0
        # SQLAlchemy 2.x: insert(...).prefix_with("OR REPLACE") for SQLite
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(table).values(batch)
        update_cols = {c.name: stmt.excluded[c.name]
                       for c in table.columns
                       if c.name != pk_col}
        stmt = stmt.on_conflict_do_update(index_elements=[pk_col],
                                          set_=update_cols)
        sess.execute(stmt)
        sess.commit()
        return len(batch)

    for r in rows:
        batch.append(r)
        if len(batch) >= INSERT_BATCH:
            n += flush(batch)
            batch.clear()
    if batch:
        n += flush(batch)
    print(f"  {label}: {n:,} rows upserted in {(time.perf_counter()-t0):.1f}s")
    return n


def _get_watermark(cache_name: str):
    """Return saved last_synced_at TIMESTAMP, or None if first run."""
    row = db.session.query(CacheWatermark).filter_by(cache_name=cache_name).first()
    return row.last_synced_at if row else None


def _set_watermark(cache_name: str, ts: datetime, rows: int) -> None:
    sess = db.session
    row = sess.query(CacheWatermark).filter_by(cache_name=cache_name).first()
    if row:
        row.last_synced_at = ts
        row.rows_last_run = rows
    else:
        sess.add(CacheWatermark(cache_name=cache_name,
                                last_synced_at=ts, rows_last_run=rows))
    sess.commit()


# ---------------------------------------------------------------------------
# Per-table loaders — each yields (model, dict) tuples
# ---------------------------------------------------------------------------

def _load_phone_lookup(client) -> int:
    sql = f"SELECT phone, usernames, match_count, is_international FROM `{PROJECT}.{DATASET}.customer_phone_lookup`"

    def gen():
        # The Dataform model GROUP BY phone *should* produce unique
        # rows, but in practice we see ~handful of dupes — likely a
        # casing/whitespace edge case in the BillPhone/ShipPhone
        # source columns. Last-wins dedupe in the loader rather than
        # blocking the entire refresh on a UNIQUE constraint failure.
        seen: set[str] = set()
        skipped = 0
        for r in client.query(sql).result():
            phone = r.phone
            if not phone:
                continue
            if phone in seen:
                skipped += 1
                continue
            seen.add(phone)
            yield {
                "phone":            phone,
                "usernames_json":   json.dumps(list(r.usernames or [])),
                "match_count":      r.match_count,
                "is_international": r.is_international,
                "cached_at":        datetime.utcnow(),
            }
        if skipped:
            print(f"  phone_lookup dedupe: skipped {skipped:,} duplicate phones")
    return _bulk_replace(CachedPhoneLookup, gen(), "phone_lookup")


def _load_related_accounts(client) -> int:
    """Mirror ``customer_related_accounts`` (email + address identity links).

    Wrapped so a missing BQ table (Dataform release not yet run) skips
    with a warning instead of failing the entire refresh — the service
    read path treats an empty cache as "no related accounts", which
    degrades cleanly.
    """
    sql = (f"SELECT Username, TO_JSON_STRING(related) AS related_json, "
           f"related_count FROM `{PROJECT}.{DATASET}.customer_related_accounts`")

    def gen():
        seen: set[str] = set()
        for r in client.query(sql).result():
            uname = r.Username
            if not uname or uname in seen:
                continue
            seen.add(uname)
            yield {
                "Username":      uname,
                "related_json":  r.related_json,
                "related_count": r.related_count,
                "cached_at":     datetime.utcnow(),
            }

    try:
        return _bulk_replace(CachedRelatedAccounts, gen(), "related_accounts")
    except Exception as exc:
        # google.api_core NotFound or any transient BQ failure — don't
        # block the other five tables.
        print(f"  related_accounts: skipped ({exc})")
        db.session.rollback()
        return 0


def _load_customer_360(client) -> int:
    """Incremental loader for customer_360.

    First run (no watermark) does a full reload — ~10 min, ~600MB.
    Subsequent runs pull only rows where ``GREATEST(last_order_date,
    last_rma_date, customer_since)`` is more recent than the saved
    watermark. Typical hourly delta: a handful of rows.

    Tradeoff: rows where all three dates are NULL (truly inactive,
    never-ordered customers) only ever come in on the first full
    reload. They don't change so this is fine. Rare profile-only edits
    on an inactive customer would be missed until the next nightly
    full reload (see ``cache_customer_360_data`` for that hook).

    BQ scan cost: a full table scan per incremental run because the
    GREATEST(...) filter can't use partition pruning. ~600MB scanned
    per hour ≈ pennies/day at on-demand pricing.
    """
    cache_name = "customer_360"
    watermark = _get_watermark(cache_name)
    is_full = watermark is None

    if is_full:
        print(f"  customer_360: full reload (no watermark)")
        sql = f"SELECT * FROM `{PROJECT}.{DATASET}.customer_360`"
        params = None
    else:
        print(f"  customer_360: incremental from {watermark.isoformat()}")
        # Date columns vs TIMESTAMP watermark — cast on the fly.
        sql = f"""
        SELECT *
        FROM `{PROJECT}.{DATASET}.customer_360`
        WHERE TIMESTAMP(GREATEST(
            IFNULL(last_order_date, DATE '1970-01-01'),
            IFNULL(last_rma_date,   DATE '1970-01-01'),
            IFNULL(customer_since,  DATE '1970-01-01')
        )) > @watermark
        """
        from google.cloud import bigquery
        params = [bigquery.ScalarQueryParameter("watermark", "TIMESTAMP", watermark)]

    def gen():
        seen: set[str] = set()
        skipped = 0
        job_config = None
        if params:
            from google.cloud import bigquery
            job_config = bigquery.QueryJobConfig(query_parameters=params)
        for r in client.query(sql, job_config=job_config).result():
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

    if is_full:
        n = _bulk_replace(CachedCustomer360, gen(), "customer_360 (full)")
    else:
        n = _bulk_upsert(CachedCustomer360, gen(), "Username",
                         "customer_360 (incremental)")
    _set_watermark(cache_name, datetime.utcnow(), n)
    return n


def _load_call_history(client) -> int:
    sql = f"SELECT * FROM `{PROJECT}.{DATASET}.call_history_360`"

    def gen():
        seen: set[str] = set()
        skipped = 0
        for r in client.query(sql).result():
            d = _row_to_dict(r)
            phone = d.get("phone")
            if not phone:
                continue
            if phone in seen:
                skipped += 1
                continue
            seen.add(phone)
            yield {
                "phone":          phone,
                "last_call_date": r.last_call_date,
                "payload_json":   json.dumps(d, default=str),
                "cached_at":      datetime.utcnow(),
            }
        if skipped:
            print(f"  call_history dedupe: skipped {skipped:,} duplicate phones")
    return _bulk_replace(CachedCallHistory, gen(), "call_history")


def _load_call_behavior(client) -> int:
    sql = f"SELECT * FROM `{PROJECT}.{DATASET}.call_behavior_360`"

    def gen():
        seen: set[str] = set()
        skipped = 0
        for r in client.query(sql).result():
            d = _row_to_dict(r)
            phone = d.get("phone")
            if not phone:
                continue
            if phone in seen:
                skipped += 1
                continue
            seen.add(phone)
            yield {
                "phone":        phone,
                "payload_json": json.dumps(d, default=str),
                "cached_at":    datetime.utcnow(),
            }
        if skipped:
            print(f"  call_behavior dedupe: skipped {skipped:,} duplicate phones")
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

    Five tables refresh in sequence:

    * ``customer_phone_lookup`` — full reload (small, fast)
    * ``call_history_360``      — full reload
    * ``call_behavior_360``     — full reload
    * ``neto_product_list``     — full reload
    * ``customer_360``          — **incremental** after the first run
      (Phase 2). Watermark stored in the ``cache_watermark`` table.

    The first ever run does a full reload of customer_360 (~10 min,
    ~600MB) since there's no saved watermark. Subsequent hourly runs
    pull only the rows whose order/RMA/customer-since date is newer
    than the saved watermark — typically a handful of rows, ~5-15s.

    Returns ``(success, message)`` for the CLI.
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
            total += _load_related_accounts(client)
            total += _load_customer_360(client)
            secs = time.perf_counter() - t0
            return True, f"customer_360 cache refreshed: {total:,} rows in {secs:.1f}s"
        except Exception as exc:
            db.session.rollback()
            return False, f"customer_360 cache refresh failed: {exc}"
