"""SQLite mirror of ``email_archive.messages`` for the Customer 360
Email History panel.

The hourly ``email-sync.timer`` already keeps ``email_archive.messages``
fresh in BigQuery. This module pulls from there into SQLite so the card
panel can render its message list and totals without the ~2s BQ round
trip from the Sydney VPS.

Refresh strategy:

* First run: full pull (~250k rows, ~5 min, ~150MB). One-off.
* Subsequent runs: incremental on ``received_at`` watermark — typically
  a handful of rows per hour.

Live top-up (``Customer360Service._live_topup``) also writes into this
cache directly so a message that arrived 30 seconds ago is visible on
the next card load without waiting for the next email-sync run.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Iterable

from flask import current_app

from app.extensions import db
from app.models.customer_cache import (
    CacheWatermark,
    CachedEmailMessage,
    CachedEmailRecipient,
)
from app.services.purchase_orders_service import purchase_orders_service

PROJECT = "chainsawspares-385722"
DATASET = "email_archive"
INSERT_BATCH = 1000

CACHE_NAME = "email_messages"


def _get_watermark():
    row = db.session.query(CacheWatermark).filter_by(cache_name=CACHE_NAME).first()
    return row.last_synced_at if row else None


def _set_watermark(ts: datetime, rows: int) -> None:
    sess = db.session
    row = sess.query(CacheWatermark).filter_by(cache_name=CACHE_NAME).first()
    if row:
        row.last_synced_at = ts
        row.rows_last_run = rows
    else:
        sess.add(CacheWatermark(cache_name=CACHE_NAME,
                                last_synced_at=ts, rows_last_run=rows))
    sess.commit()


def _truncate(model) -> None:
    db.session.execute(db.delete(model))
    db.session.commit()


def _row_to_message(r) -> dict:
    return {
        "message_id":         r.message_id,
        "conversation_id":    r.conversation_id,
        "from_address":       (r.from_address or "").lower() or None,
        "from_name":          r.from_name,
        "subject":            r.subject,
        "received_at":        r.received_at,
        "direction":          r.direction,
        "is_automated":       bool(r.is_automated) if r.is_automated is not None else None,
        "has_attachments":    bool(r.has_attachments) if r.has_attachments is not None else None,
        "body_preview":       (r.body_preview or "")[:200] or None,
        "parent_folder_name": r.parent_folder_name,
        "web_link":           r.web_link,
        "cached_at":          datetime.utcnow(),
    }


def _row_to_recipients(r) -> list[dict]:
    out: list[dict] = []
    for arr_attr in ("to_addresses", "cc_addresses", "bcc_addresses"):
        addrs = getattr(r, arr_attr, None)
        if not addrs:
            continue
        for a in addrs:
            if a:
                out.append({"message_id": r.message_id, "address": a.strip().lower()})
    return out


def cache_email_archive() -> tuple[bool, str]:
    """Refresh the email cache (full first run, incremental after).

    Run from ``flask refresh-cache`` after the customer_360 caches.
    Independent of the BQ-side ``email-sync.timer`` which keeps
    ``email_archive.messages`` itself fresh in BigQuery.
    """
    app = current_app._get_current_object()
    with app.app_context():
        client = purchase_orders_service.client
        if client is None:
            return False, "BigQuery client not available"
        try:
            return _do_refresh(client)
        except Exception as exc:
            db.session.rollback()
            return False, f"email_messages cache refresh failed: {exc}"


def _do_refresh(client) -> tuple[bool, str]:
    t0 = time.perf_counter()
    print("Refreshing email_messages cache from BigQuery...")
    watermark = _get_watermark()
    is_full = watermark is None

    if is_full:
        print(f"  email_messages: full reload (no watermark)")
        sql = f"""
        SELECT
          message_id, conversation_id, from_address, from_name, subject,
          received_at, direction, is_automated, has_attachments,
          body_preview, parent_folder_name, web_link,
          to_addresses, cc_addresses, bcc_addresses
        FROM `{PROJECT}.{DATASET}.messages`
        """
        params = None
        # Truncate first — fresh state
        _truncate(CachedEmailRecipient)
        _truncate(CachedEmailMessage)
    else:
        print(f"  email_messages: incremental from {watermark.isoformat()}")
        sql = f"""
        SELECT
          message_id, conversation_id, from_address, from_name, subject,
          received_at, direction, is_automated, has_attachments,
          body_preview, parent_folder_name, web_link,
          to_addresses, cc_addresses, bcc_addresses
        FROM `{PROJECT}.{DATASET}.messages`
        WHERE received_at > @watermark
        """
        from google.cloud import bigquery
        params = [bigquery.ScalarQueryParameter("watermark", "TIMESTAMP", watermark)]

    job_config = None
    if params:
        from google.cloud import bigquery
        job_config = bigquery.QueryJobConfig(query_parameters=params)

    sess = db.session
    msg_batch: list[dict] = []
    rcp_batch: list[dict] = []
    total_msgs = 0
    seen: set[str] = set()
    max_received: datetime | None = None

    def flush_messages():
        nonlocal msg_batch
        if not msg_batch:
            return 0
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(CachedEmailMessage.__table__).values(msg_batch)
        update_cols = {c.name: stmt.excluded[c.name]
                       for c in CachedEmailMessage.__table__.columns
                       if c.name != "message_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["message_id"], set_=update_cols)
        sess.execute(stmt)
        sess.commit()
        n = len(msg_batch)
        msg_batch = []
        return n

    def flush_recipients():
        nonlocal rcp_batch
        if not rcp_batch:
            return 0
        # Recipient table has an autoincrement PK; we can't upsert by
        # (message_id, address) without a composite unique index. Two
        # passes: first delete the old recipient rows for any message
        # we're inserting/updating (incremental case only), then bulk-
        # insert. On full-load we already truncated.
        sess.bulk_insert_mappings(CachedEmailRecipient, rcp_batch)
        sess.commit()
        n = len(rcp_batch)
        rcp_batch = []
        return n

    for r in client.query(sql, job_config=job_config).result():
        mid = r.message_id
        if not mid or mid in seen:
            continue
        seen.add(mid)

        if not is_full:
            # Incremental: drop any previously-cached recipient rows
            # for this message to avoid duplicating on a content change
            # (e.g. recipient list edit). Cheap because indexed on
            # message_id.
            sess.execute(db.delete(CachedEmailRecipient).where(
                CachedEmailRecipient.message_id == mid
            ))

        msg_batch.append(_row_to_message(r))
        rcp_batch.extend(_row_to_recipients(r))
        if r.received_at and (max_received is None or r.received_at > max_received):
            max_received = r.received_at

        if len(msg_batch) >= INSERT_BATCH:
            total_msgs += flush_messages()
            flush_recipients()
    total_msgs += flush_messages()
    flush_recipients()

    new_watermark = max_received or datetime.utcnow()
    _set_watermark(new_watermark, total_msgs)

    secs = time.perf_counter() - t0
    return True, f"email_messages cache refreshed: {total_msgs:,} messages in {secs:.1f}s"


# ---------------------------------------------------------------------------
# Public hook for live top-up — called by Customer360Service._live_topup
# after a fresh insert into BQ, to make the message visible on next card load.
# ---------------------------------------------------------------------------

def upsert_message(payload: dict) -> None:
    """Insert/update one message into the local cache from a Graph payload.

    ``payload`` is the dict shape produced by ``email_backfill.to_row``
    so the keys line up with our column names. We drop body_text/body_html
    and slice body_preview to 200 chars before insert.
    """
    if not payload or not payload.get("message_id"):
        return
    sess = db.session
    try:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        msg = {
            "message_id":         payload["message_id"],
            "conversation_id":    payload.get("conversation_id"),
            "from_address":       (payload.get("from_address") or "").lower() or None,
            "from_name":          payload.get("from_name"),
            "subject":            payload.get("subject"),
            "received_at":        payload.get("received_at"),
            "direction":          payload.get("direction"),
            "is_automated":       payload.get("is_automated"),
            "has_attachments":    payload.get("has_attachments"),
            "body_preview":       (payload.get("body_preview") or "")[:200] or None,
            "parent_folder_name": payload.get("parent_folder_name"),
            "web_link":           payload.get("web_link"),
            "cached_at":          datetime.utcnow(),
        }
        # Coerce ISO string to datetime if needed
        if isinstance(msg["received_at"], str):
            try:
                msg["received_at"] = datetime.fromisoformat(
                    msg["received_at"].replace("Z", "+00:00"))
            except Exception:
                msg["received_at"] = None

        stmt = sqlite_insert(CachedEmailMessage.__table__).values(**msg)
        update_cols = {c.name: stmt.excluded[c.name]
                       for c in CachedEmailMessage.__table__.columns
                       if c.name != "message_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["message_id"], set_=update_cols)
        sess.execute(stmt)

        # Refresh recipient rows for this message
        sess.execute(db.delete(CachedEmailRecipient).where(
            CachedEmailRecipient.message_id == payload["message_id"]
        ))
        rcps: list[dict] = []
        for fld in ("to_addresses", "cc_addresses", "bcc_addresses"):
            for a in (payload.get(fld) or []):
                if a:
                    rcps.append({"message_id": payload["message_id"],
                                 "address": a.strip().lower()})
        if rcps:
            sess.bulk_insert_mappings(CachedEmailRecipient, rcps)
        sess.commit()
    except Exception:
        sess.rollback()
