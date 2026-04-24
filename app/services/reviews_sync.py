"""Bidirectional sync between local ``ItemReview`` rows and the BigQuery
``item_reviews`` table."""
from __future__ import annotations

import json
from datetime import datetime

from app.extensions import db
from app.models.reviews import ItemReview
from app.services.purchase_orders_service import purchase_orders_service
from app.utils.dates import parse_iso_datetime


def sync_review_to_bigquery(review: ItemReview) -> None:
    """Best-effort upload of a single review row to BigQuery."""
    try:
        snapshot = None
        if review.comparison_snapshot:
            try:
                json.loads(review.comparison_snapshot)
                snapshot = review.comparison_snapshot
            except json.JSONDecodeError:
                snapshot = None
        payload = {
            "review_id": review.review_id,
            "po_id": review.po_id,
            "order_id": review.order_id,
            "po_item_id": review.po_item_id,
            "sku": review.sku,
            "flagged_by": review.flagged_by,
            "flagged_at": review.flagged_at.isoformat() if review.flagged_at else None,
            "flag_comment": review.flag_comment,
            "status": review.status,
            "warehouse_assigned_to": review.warehouse_assigned_to,
            "warehouse_started_at": review.warehouse_started_at.isoformat()
            if review.warehouse_started_at
            else None,
            "warehouse_comment": review.warehouse_comment,
            "warehouse_closed_at": review.warehouse_closed_at.isoformat()
            if review.warehouse_closed_at
            else None,
            "retail_closed_by": review.retail_closed_by,
            "retail_closed_at": review.retail_closed_at.isoformat() if review.retail_closed_at else None,
            "retail_comment": review.retail_comment,
            "comparison_snapshot": snapshot,
            "updated_at": datetime.utcnow().isoformat(),
        }
        success, error = purchase_orders_service.insert_item_review(payload)
        if not success and error:
            print(f"Warning: failed to insert review into BigQuery: {error}")
    except Exception as e:
        print(f"Warning: could not sync review to BigQuery: {str(e)}")


def sync_reviews_from_bigquery() -> tuple[bool, str]:
    """Pull all reviews from BigQuery into local SQLite, deduping on review_id."""
    if not purchase_orders_service.client:
        print("BigQuery client not initialized; skipping review sync.")
        return False, "BigQuery client not initialized"
    try:
        reviews, error = purchase_orders_service.get_all_item_reviews()
        if error:
            print(f"Error fetching reviews from BigQuery: {error}")
            return False, error

        STATUS_PRIORITY = {
            s: i
            for i, s in enumerate(
                ["pending", "warehouse_in_progress", "warehouse_closed", "retail_closed", "cancelled"]
            )
        }
        best_by_id: dict[str, dict] = {}
        for record in reviews:
            review_uuid = record.get("review_id")
            if not review_uuid:
                continue
            prev = best_by_id.get(review_uuid)
            if prev is None:
                best_by_id[review_uuid] = record
            else:
                prev_ts = prev.get("updated_at") or ""
                curr_ts = record.get("updated_at") or ""
                prev_pri = STATUS_PRIORITY.get(prev.get("status"), -1)
                curr_pri = STATUS_PRIORITY.get(record.get("status"), -1)
                if curr_ts > prev_ts or (curr_ts == prev_ts and curr_pri > prev_pri):
                    best_by_id[review_uuid] = record
        skipped_duplicates = len(reviews) - len(best_by_id)

        ItemReview.query.delete()
        db.session.commit()
        for review_uuid, record in best_by_id.items():
            review = ItemReview(
                review_id=review_uuid,
                po_id=record.get("po_id"),
                order_id=record.get("order_id"),
                po_item_id=record.get("po_item_id"),
                sku=record.get("sku"),
                flagged_by=record.get("flagged_by"),
                flagged_at=parse_iso_datetime(record.get("flagged_at")),
                flag_comment=record.get("flag_comment"),
                status=record.get("status"),
                warehouse_assigned_to=record.get("warehouse_assigned_to"),
                warehouse_started_at=parse_iso_datetime(record.get("warehouse_started_at")),
                warehouse_comment=record.get("warehouse_comment"),
                warehouse_closed_at=parse_iso_datetime(record.get("warehouse_closed_at")),
                retail_closed_by=record.get("retail_closed_by"),
                retail_closed_at=parse_iso_datetime(record.get("retail_closed_at")),
                retail_comment=record.get("retail_comment"),
                comparison_snapshot=(
                    record.get("comparison_snapshot")
                    if isinstance(record.get("comparison_snapshot"), str)
                    else json.dumps(record.get("comparison_snapshot"))
                    if record.get("comparison_snapshot")
                    else None
                ),
                updated_at=parse_iso_datetime(record.get("updated_at")),
            )
            db.session.add(review)
        db.session.commit()
        synced_count = len(best_by_id)
        if skipped_duplicates:
            print(f"Skipped {skipped_duplicates} duplicate review rows from BigQuery.")
        print(f"Synced {synced_count} reviews from BigQuery")
        return True, f"Synced {synced_count} reviews."
    except Exception as e:
        db.session.rollback()
        print(f"Error syncing reviews: {str(e)}")
        return False, str(e)
