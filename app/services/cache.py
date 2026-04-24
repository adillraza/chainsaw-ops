"""Local SQLite cache of BigQuery purchase-order data.

This is a near-verbatim move of the cache helpers that used to live inside
``app.py``. Behaviour is preserved (same function names, same logging output)
so the legacy ``/api/bigquery/*`` routes need no changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from flask import current_app

from app.extensions import db
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.services.purchase_orders_service import purchase_orders_service
from app.services.sync_state import sync_state_service
from app.utils.dates import convert_decimal_to_float, safe_parse_date


def update_cache_with_latest_note(po_item_id, po_id):
    """Refresh the cached note columns for a single item from BigQuery."""
    try:
        latest_note_data, error = purchase_orders_service.get_item_notes(po_item_id)

        if not error and latest_note_data and len(latest_note_data) > 0:
            latest_note = latest_note_data[0]
            note_text = latest_note.get("comment", "")
            note_user = latest_note.get("username", "admin")
            note_date = latest_note.get("created_at")
        else:
            note_text = None
            note_user = None
            note_date = None

        cached_comparison = CachedPurchaseOrderComparison.query.filter(
            CachedPurchaseOrderComparison.po_item_id == str(po_item_id)
        ).first()

        if cached_comparison:
            cached_comparison.latest_item_note = note_text
            cached_comparison.latest_item_note_user = note_user
            cached_comparison.latest_item_note_date = safe_parse_date(note_date) if note_date else None
            db.session.commit()
            print(f"Updated comparison cache for po_item_id {po_item_id} with latest note")

        cached_item = CachedPurchaseOrderItem.query.filter(
            CachedPurchaseOrderItem.po_item_id == str(po_item_id)
        ).first()

        if cached_item:
            cached_item.latest_item_note = note_text
            cached_item.latest_item_note_user = note_user
            cached_item.latest_item_note_date = safe_parse_date(note_date) if note_date else None
            db.session.commit()
            print(f"Updated items cache for po_item_id {po_item_id} with latest note")

        return True

    except Exception as e:
        print(f"Error updating cache for po_item_id {po_item_id}: {str(e)}")
        return False


def refresh_po_cache(po_id):
    """No-op: kept for legacy callers. Notes update live via JS now."""
    try:
        print(f"Cache refresh not needed for PO {po_id} - notes update handled in real-time")
        return True
    except Exception as e:
        print(f"Error refreshing cache for PO {po_id}: {str(e)}")
        return False


def cache_purchase_order_data():
    """Fetch all PO data from BigQuery and cache it locally.

    Background-thread safe via :class:`SyncStateService`. Run with an
    application context (the ``/api/system/start-refresh`` route spawns a
    thread that uses the captured app object).
    """
    app = current_app._get_current_object()
    with app.app_context():
        try:
            if not sync_state_service.start():
                print("Sync already in progress, skipping...")
                return False, "Sync already in progress"

            print("Starting to cache all purchase order data...")

            print("Clearing existing cache...")
            CachedPurchaseOrderSummary.query.delete()
            CachedPurchaseOrderItem.query.delete()
            CachedPurchaseOrderComparison.query.delete()
            db.session.commit()
            print("Cache cleared successfully")

            if sync_state_service.should_stop:
                print("Sync cancelled by user")
                sync_state_service.finish()
                return False, "Sync cancelled"

            print("Getting actual data counts from BigQuery...")
            summary_count, error = purchase_orders_service.get_summary_count()
            if error:
                print(f"Error getting summary count: {error}")
                sync_state_service.finish()
                return False, error

            items_count, error = purchase_orders_service.get_items_count()
            if error:
                print(f"Error getting items count: {error}")
                sync_state_service.finish()
                return False, error

            comparison_count, error = purchase_orders_service.get_comparison_count()
            if error:
                print(f"Error getting comparison count: {error}")
                sync_state_service.finish()
                return False, error

            print(f"Actual counts - Summary: {summary_count}, Items: {items_count}, Comparison: {comparison_count}")

            sync_state_service.set_totals(
                summary=summary_count,
                items=items_count,
                comparison=comparison_count,
            )

            summary_data, error = purchase_orders_service.get_purchase_order_summary(limit=None, offset=0)
            if error:
                print(f"Error fetching summary data: {error}")
                sync_state_service.finish()
                return False, error

            print(f"Fetched {len(summary_data)} summary records from BigQuery")

            print("Caching summary data...")
            summary_cached = 0
            batch_size = 100
            for i in range(0, len(summary_data), batch_size):
                if sync_state_service.should_stop:
                    print("Sync cancelled during summary caching")
                    sync_state_service.finish()
                    return False, "Sync cancelled"

                batch = summary_data[i : i + batch_size]
                for row in batch:
                    try:
                        cached_row = CachedPurchaseOrderSummary(
                            po_id=row.get("po_id"),
                            po_status=row.get("po_status"),
                            rex_po_created_by=row.get("rex_po_created_by"),
                            received_by=row.get("received_by"),
                            supplier=row.get("supplier"),
                            requested_date=safe_parse_date(row.get("requested_date")),
                            order_id=row.get("OrderID"),
                            order_link=row.get("order_link"),
                            entered_date=safe_parse_date(row.get("entered_date")),
                            received_date=safe_parse_date(row.get("received_date")),
                            neto_order_created_by=row.get("neto_order_created_by"),
                            completed_date=safe_parse_date(row.get("completed_date")),
                            completion_status=row.get("completion_status"),
                            order_status=row.get("order_status"),
                            difference=convert_decimal_to_float(row.get("difference")),
                            disparity=row.get("disparity"),
                            item_count=row.get("item_count"),
                            total_quantity_ordered=convert_decimal_to_float(row.get("total_quantity_ordered")),
                            total_quantity_received=convert_decimal_to_float(row.get("total_quantity_received")),
                            total_rex_cost=convert_decimal_to_float(row.get("total_rex_cost")),
                            total_neto_cost=convert_decimal_to_float(row.get("total_neto_cost")),
                            latest_po_note=row.get("latest_po_note"),
                            latest_po_note_user=row.get("latest_po_note_user"),
                            latest_po_note_date=safe_parse_date(row.get("latest_po_note_date")),
                            no_of_neto_orders=row.get("no_of_neto_orders"),
                            neto_order_ids=row.get("neto_order_ids"),
                        )
                        db.session.add(cached_row)
                        summary_cached += 1
                    except Exception as e:
                        print(f"Error caching summary row: {str(e)}")
                        continue

                db.session.commit()
                print(f"Cached {summary_cached}/{len(summary_data)} summary records...")

            print(f"Successfully cached {summary_cached} summary records")

            print("Fetching all items data...")
            items_data, error = purchase_orders_service.get_all_purchase_order_items()
            if error:
                print(f"Error fetching items data: {error}")
                sync_state_service.finish()
                return False, f"Summary cached but items failed: {error}"

            print(f"Fetched {len(items_data)} items records from BigQuery")

            print("Caching items data...")
            items_cached = 0
            batch_size = 100
            for i in range(0, len(items_data), batch_size):
                if sync_state_service.should_stop:
                    print("Sync cancelled during items caching")
                    sync_state_service.finish()
                    return False, "Sync cancelled"

                batch = items_data[i : i + batch_size]
                for item in batch:
                    try:
                        cached_item = CachedPurchaseOrderItem(
                            po_id=item.get("po_id"),
                            po_item_id=item.get("po_item_id"),
                            sku=item.get("sku"),
                            supplier_sku=item.get("supplier_sku"),
                            manufacturer_sku=item.get("manufacturer_sku"),
                            short_description=item.get("short_description"),
                            neto_qty_ordered=convert_decimal_to_float(item.get("neto_qty_ordered")),
                            rex_qty_ordered=convert_decimal_to_float(item.get("rex_qty_ordered")),
                            rex_qty_received=convert_decimal_to_float(item.get("rex_qty_received")),
                            neto_qty_available=item.get("neto_qty_available"),
                            neto_cost_price=convert_decimal_to_float(item.get("neto_cost_price")),
                            rex_supplier_buy_ex=convert_decimal_to_float(item.get("rex_supplier_buy_ex")),
                            difference=convert_decimal_to_float(item.get("difference")),
                            disparity=item.get("disparity"),
                            order_id=item.get("OrderID"),
                            created_on=safe_parse_date(item.get("created_on")),
                            modified_on=safe_parse_date(item.get("modified_on")),
                            latest_item_note=item.get("latest_item_note"),
                            latest_item_note_user=item.get("latest_item_note_user"),
                            latest_item_note_date=safe_parse_date(item.get("latest_item_note_date")),
                            neto_product_id=item.get("neto_product_id"),
                            is_kitted_item=item.get("is_kitted_item"),
                            cached_at=datetime.utcnow(),
                        )
                        db.session.add(cached_item)
                        items_cached += 1
                    except Exception as e:
                        print(f"Error caching item: {str(e)}")
                        continue

                db.session.commit()
                print(f"Cached {items_cached}/{len(items_data)} items records...")

            print(f"Successfully cached {items_cached} items records")

            print("Fetching all comparison data...")
            comparison_data, error = purchase_orders_service.get_all_purchase_order_comparison()
            if error:
                print(f"Error fetching comparison data: {error}")
                sync_state_service.finish()
                return False, f"Summary and items cached but comparison failed: {error}"

            print(f"Fetched {len(comparison_data)} comparison records from BigQuery")

            print("Caching comparison data...")
            comparison_cached = 0
            batch_size = 100
            for i in range(0, len(comparison_data), batch_size):
                if sync_state_service.should_stop:
                    print("Sync cancelled during comparison caching")
                    sync_state_service.finish()
                    return False, "Sync cancelled"

                batch = comparison_data[i : i + batch_size]
                for comp in batch:
                    try:
                        cached_comp = CachedPurchaseOrderComparison(
                            po_id=convert_decimal_to_float(comp.get("po_id")),
                            modified_on=safe_parse_date(comp.get("modified_on")),
                            sku=comp.get("sku"),
                            name=comp.get("name"),
                            change_log=comp.get("change_log"),
                            rex_available_qty=convert_decimal_to_float(comp.get("rex_available_qty")),
                            neto_qty_available=convert_decimal_to_float(comp.get("neto_qty_available")),
                            original_rex_qty_ordered=convert_decimal_to_float(comp.get("original_rex_qty_ordered")),
                            neto_qty_shipped=convert_decimal_to_float(comp.get("neto_qty_shipped")),
                            final_rex_qty_ordered=convert_decimal_to_float(comp.get("final_rex_qty_ordered")),
                            rex_qty_received=convert_decimal_to_float(comp.get("rex_qty_received")),
                            order_id=comp.get("OrderID"),
                            po_item_id=comp.get("po_item_id"),
                            latest_item_note=comp.get("latest_item_note"),
                            latest_item_note_user=comp.get("latest_item_note_user"),
                            latest_item_note_date=safe_parse_date(comp.get("latest_item_note_date")),
                            neto_product_id=comp.get("neto_product_id"),
                            is_kitted_item=comp.get("is_kitted_item"),
                            cached_at=datetime.utcnow(),
                        )
                        db.session.add(cached_comp)
                        comparison_cached += 1
                    except Exception as e:
                        print(f"Error caching comparison: {str(e)}")
                        continue

                db.session.commit()
                print(f"Cached {comparison_cached}/{len(comparison_data)} comparison records...")

            print(f"Successfully cached {comparison_cached} comparison records")

            sync_state_service.finish()

            return True, (
                f"Cached {summary_cached} summary, {items_cached} items, "
                f"and {comparison_cached} comparison records"
            )

        except Exception as e:
            print(f"Error caching data: {str(e)}")
            db.session.rollback()
            sync_state_service.finish()
            return False, str(e)


def get_cached_summary_data(search_term=None):
    """Return up to 200 most-recent summary rows, optionally filtered."""
    query = CachedPurchaseOrderSummary.query

    if search_term:
        query = query.filter(
            db.or_(
                CachedPurchaseOrderSummary.po_id.like(f"%{search_term}%"),
                CachedPurchaseOrderSummary.order_id.like(f"%{search_term}%"),
            )
        )

    query = query.order_by(CachedPurchaseOrderSummary.po_id.desc())

    cached_records = query.limit(200).all()
    return [record.to_dict() for record in cached_records]


def get_cached_items_data(po_id=None, order_id=None):
    """Return cached item rows for a PO or NETO order id."""
    query = CachedPurchaseOrderItem.query

    if po_id:
        query = query.filter(CachedPurchaseOrderItem.po_id == po_id)
    elif order_id:
        query = query.filter(CachedPurchaseOrderItem.order_id == order_id)

    query = query.order_by(CachedPurchaseOrderItem.po_item_id)

    cached_records = query.all()
    return [record.to_dict() for record in cached_records]


def cache_items_data(po_id, order_id, items_data: Iterable[dict]) -> bool:
    """Replace cached items for a PO/Order with the given BigQuery rows."""
    try:
        if po_id:
            CachedPurchaseOrderItem.query.filter(CachedPurchaseOrderItem.po_id == po_id).delete()
        if order_id:
            CachedPurchaseOrderItem.query.filter(CachedPurchaseOrderItem.order_id == order_id).delete()

        items_data = list(items_data)
        for item in items_data:
            cached_item = CachedPurchaseOrderItem(
                po_id=item.get("po_id"),
                po_item_id=item.get("po_item_id"),
                sku=item.get("sku"),
                supplier_sku=item.get("supplier_sku"),
                manufacturer_sku=item.get("manufacturer_sku"),
                short_description=item.get("short_description"),
                neto_qty_ordered=convert_decimal_to_float(item.get("neto_qty_ordered")),
                rex_qty_ordered=convert_decimal_to_float(item.get("rex_qty_ordered")),
                rex_qty_received=convert_decimal_to_float(item.get("rex_qty_received")),
                neto_qty_available=item.get("neto_qty_available"),
                neto_cost_price=convert_decimal_to_float(item.get("neto_cost_price")),
                rex_supplier_buy_ex=convert_decimal_to_float(item.get("rex_supplier_buy_ex")),
                difference=convert_decimal_to_float(item.get("difference")),
                disparity=item.get("disparity"),
                order_id=item.get("OrderID"),
                created_on=safe_parse_date(item.get("created_on")),
                modified_on=safe_parse_date(item.get("modified_on")),
                latest_item_note=item.get("latest_item_note"),
                latest_item_note_user=item.get("latest_item_note_user"),
                latest_item_note_date=safe_parse_date(item.get("latest_item_note_date")),
                neto_product_id=item.get("neto_product_id"),
                is_kitted_item=item.get("is_kitted_item"),
            )
            db.session.add(cached_item)

        db.session.commit()
        print(f"Cached {len(items_data)} items for PO {po_id}")
        return True
    except Exception as e:
        print(f"Error caching items data: {str(e)}")
        db.session.rollback()
        return False


def get_cached_comparison_data(po_id=None, order_id=None):
    """Return cached comparison rows for a PO or NETO order id."""
    query = CachedPurchaseOrderComparison.query

    if po_id:
        query = query.filter(CachedPurchaseOrderComparison.po_id == po_id)
    elif order_id:
        query = query.filter(CachedPurchaseOrderComparison.order_id == order_id)

    query = query.order_by(CachedPurchaseOrderComparison.modified_on.desc())

    cached_records = query.all()
    return [record.to_dict() for record in cached_records]


def cache_comparison_data(po_id, order_id, comparison_data: Iterable[dict]) -> bool:
    """Replace cached comparison rows for a PO/Order with BigQuery rows."""
    try:
        if po_id:
            CachedPurchaseOrderComparison.query.filter(
                CachedPurchaseOrderComparison.po_id == po_id
            ).delete()
        if order_id:
            CachedPurchaseOrderComparison.query.filter(
                CachedPurchaseOrderComparison.order_id == order_id
            ).delete()

        comparison_data = list(comparison_data)
        for item in comparison_data:
            cached_item = CachedPurchaseOrderComparison(
                po_id=convert_decimal_to_float(item.get("po_id")),
                modified_on=safe_parse_date(item.get("modified_on")) if item.get("modified_on") else None,
                sku=item.get("sku"),
                name=item.get("name"),
                change_log=item.get("change_log"),
                rex_available_qty=convert_decimal_to_float(item.get("rex_available_qty")),
                neto_qty_available=convert_decimal_to_float(item.get("neto_qty_available")),
                original_rex_qty_ordered=convert_decimal_to_float(item.get("original_rex_qty_ordered")),
                neto_qty_shipped=convert_decimal_to_float(item.get("neto_qty_shipped")),
                final_rex_qty_ordered=convert_decimal_to_float(item.get("final_rex_qty_ordered")),
                rex_qty_received=convert_decimal_to_float(item.get("rex_qty_received")),
                order_id=item.get("OrderID"),
                po_item_id=item.get("po_item_id"),
                latest_item_note=item.get("latest_item_note"),
                latest_item_note_user=item.get("latest_item_note_user"),
                latest_item_note_date=safe_parse_date(item.get("latest_item_note_date"))
                if item.get("latest_item_note_date")
                else None,
                neto_product_id=item.get("neto_product_id"),
                is_kitted_item=item.get("is_kitted_item"),
            )
            db.session.add(cached_item)

        db.session.commit()
        print(f"Cached {len(comparison_data)} comparison records for PO {po_id}")
        return True
    except Exception as e:
        print(f"Error caching comparison data: {str(e)}")
        db.session.rollback()
        return False


def enrich_comparison_data_with_notes(data):
    """Backfill ``latest_item_note*`` columns from BigQuery for live data."""
    try:
        enriched_data = []
        for item in data:
            po_item_id = item.get("po_item_id")
            if po_item_id:
                notes, error = purchase_orders_service.get_item_notes(po_item_id)
                if not error and notes and len(notes) > 0:
                    latest_note = notes[0]
                    item["latest_item_note"] = latest_note.get("comment")
                    item["latest_item_note_user"] = latest_note.get("username")
                    item["latest_item_note_date"] = latest_note.get("created_at")
                else:
                    item["latest_item_note"] = None
                    item["latest_item_note_user"] = None
                    item["latest_item_note_date"] = None
            else:
                item["latest_item_note"] = None
                item["latest_item_note_user"] = None
                item["latest_item_note_date"] = None

            enriched_data.append(item)

        return enriched_data
    except Exception as e:
        print(f"Error enriching comparison data with notes: {str(e)}")
        return data
