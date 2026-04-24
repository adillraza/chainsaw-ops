"""BigQuery / cache JSON endpoints (search, refresh, status).

Cache-management endpoints (``cache-status``, ``start-refresh``,
``clear-cache``) live in :mod:`app.blueprints.system_api` and are exposed
under ``/api/system/*``. The wrappers here just delegate so any older client
hitting ``/api/bigquery/*`` keeps working until those callers are updated.
"""
from __future__ import annotations

import time
from datetime import datetime

from flask import jsonify, request
from flask_login import login_required

from app.blueprints.legacy_api import legacy_api_bp
from app.blueprints.system_api.routes import (
    cache_status as _system_cache_status,
    clear_cache as _system_clear_cache,
    start_background_refresh as _system_start_refresh,
)
from app.models.reviews import ItemReview
from app.services.cache import (
    cache_comparison_data,
    cache_items_data,
    cache_purchase_order_data,
    get_cached_comparison_data,
    get_cached_items_data,
    get_cached_summary_data,
)
from app.services.purchase_orders_service import purchase_orders_service
from app.services.reviews_sync import sync_reviews_from_bigquery


@legacy_api_bp.route("/bigquery/test")
@login_required
def test_bigquery_connection():
    success, message = purchase_orders_service.test_connection()
    return jsonify({"success": success, "message": message})


@legacy_api_bp.route("/bigquery/schema")
@login_required
def get_bigquery_schema():
    """Return the BigQuery table schema (best-effort)."""
    schema, error = purchase_orders_service.get_table_schema()
    if error:
        return jsonify({"success": False, "error": error})
    return jsonify({"success": True, "schema": schema})


@legacy_api_bp.route("/bigquery/summary")
@login_required
def get_bigquery_summary():
    search_term = request.args.get("search", None)
    sku_search = request.args.get("sku_search", None)

    if sku_search:
        try:
            print(f"SKU search requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_purchase_order_summary_by_sku(sku_search)
            if not error and bigquery_data:
                return jsonify({
                    "success": True,
                    "data": bigquery_data,
                    "count": len(bigquery_data),
                    "total_count": len(bigquery_data),
                    "sku_search": sku_search,
                    "timestamp": datetime.utcnow().isoformat(),
                })
            print(f"SKU search failed: {error}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search failed: {error}",
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"SKU search error: {str(e)}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat(),
            })

    data = get_cached_summary_data(search_term)

    if not data:
        try:
            print("No cached data found, fetching from BigQuery as fallback")
            bigquery_data, error = purchase_orders_service.get_purchase_order_summary(
                limit=None, offset=0, search_term=search_term
            )
            if not error and bigquery_data:
                data = bigquery_data
            else:
                print(f"BigQuery fallback failed: {error}")
        except Exception as e:
            print(f"BigQuery fallback error: {str(e)}")

    return jsonify({
        "success": True,
        "data": data,
        "count": len(data),
        "total_count": len(data),
        "search_term": search_term,
        "timestamp": datetime.utcnow().isoformat(),
    })


@legacy_api_bp.route("/bigquery/items")
@login_required
def get_bigquery_items():
    start_time = time.time()

    po_id = request.args.get("po_id", None)
    order_id = request.args.get("order_id", None)
    sku_search = request.args.get("sku_search", None)

    if sku_search:
        try:
            print(f"SKU search for items requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_all_purchase_order_items_by_sku(sku_search)
            if not error and bigquery_data:
                return jsonify({
                    "success": True,
                    "data": bigquery_data,
                    "count": len(bigquery_data),
                    "sku_search": sku_search,
                    "timestamp": datetime.utcnow().isoformat(),
                })
            print(f"SKU search for items failed: {error}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search for items failed: {error}",
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"SKU search for items error: {str(e)}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search for items error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat(),
            })

    data = get_cached_items_data(po_id, order_id)
    from_cache = True

    if not data:
        from_cache = False
        try:
            print(f"No cached items found for PO {po_id}, fetching from BigQuery and caching")
            bigquery_data, error = purchase_orders_service.get_purchase_order_items(
                po_id, order_id, limit=None, offset=0
            )
            if not error and bigquery_data:
                cache_items_data(po_id, order_id, bigquery_data)
                data = bigquery_data
            else:
                print(f"BigQuery items fetch failed: {error}")
        except Exception as e:
            print(f"BigQuery items fetch error: {str(e)}")

    load_time = int((time.time() - start_time) * 1000)

    return jsonify({
        "success": True,
        "data": data,
        "count": len(data),
        "total_count": len(data),
        "po_id": po_id,
        "order_id": order_id,
        "from_cache": from_cache,
        "load_time": load_time,
        "timestamp": datetime.utcnow().isoformat(),
    })


@legacy_api_bp.route("/bigquery/comparison")
@login_required
def get_bigquery_comparison():
    start_time = time.time()

    po_id = request.args.get("po_id", None)
    order_id = request.args.get("order_id", None)
    sku_search = request.args.get("sku_search", None)

    if sku_search:
        try:
            print(f"SKU search for comparison requested: {sku_search}")
            bigquery_data, error = purchase_orders_service.get_all_purchase_order_comparison_by_sku(
                sku_search
            )
            if not error and bigquery_data:
                load_time = int((time.time() - start_time) * 1000)
                return jsonify({
                    "success": True,
                    "data": bigquery_data,
                    "count": len(bigquery_data),
                    "sku_search": sku_search,
                    "from_cache": False,
                    "load_time": load_time,
                    "timestamp": datetime.utcnow().isoformat(),
                    "reviews": [],
                })
            print(f"SKU search for comparison failed: {error}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search for comparison failed: {error}",
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"SKU search for comparison error: {str(e)}")
            return jsonify({
                "success": False,
                "data": [],
                "message": f"SKU search for comparison error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat(),
            })

    data = get_cached_comparison_data(po_id, order_id)
    from_cache = True

    if data and len(data) > 0:
        print(
            f"API: Serving cached comparison data for PO {po_id}, "
            f"first record po_item_id: {data[0].get('po_item_id', 'MISSING')}"
        )

    if not data or (data and len(data) > 0 and data[0].get("po_item_id") is None):
        from_cache = False
        try:
            print(f"No cached comparison data found for PO {po_id}, fetching from BigQuery and caching")
            bigquery_data, error = purchase_orders_service.get_purchase_order_comparison(
                po_id, order_id, limit=None, offset=0
            )
            if not error and bigquery_data:
                cache_comparison_data(po_id, order_id, bigquery_data)
                data = bigquery_data
            else:
                print(f"BigQuery comparison fetch failed: {error}")
        except Exception as e:
            print(f"BigQuery comparison fetch error: {str(e)}")

    load_time = int((time.time() - start_time) * 1000)

    review_records = ItemReview.query.filter(ItemReview.po_id == po_id).all() if po_id else []
    review_statuses = [
        {
            "review_id": review.review_id,
            "po_item_id": review.po_item_id,
            "status": review.status,
            "flagged_by": review.flagged_by,
            "flagged_at": review.flagged_at.isoformat() if review.flagged_at else None,
        }
        for review in review_records
    ]

    return jsonify({
        "success": True,
        "data": data,
        "count": len(data),
        "total_count": len(data),
        "po_id": po_id,
        "order_id": order_id,
        "from_cache": from_cache,
        "load_time": load_time,
        "timestamp": datetime.utcnow().isoformat(),
        "reviews": review_statuses,
    })


@legacy_api_bp.route("/bigquery/refresh", methods=["POST"])
@login_required
def refresh_bigquery_data():
    try:
        success, message = cache_purchase_order_data()
        if success:
            review_success, review_msg = sync_reviews_from_bigquery()
            combined_msg = message
            if review_success:
                combined_msg += f" {review_msg}"
            else:
                combined_msg += f" (Review sync warning: {review_msg})"
            return jsonify({"success": True, "message": combined_msg})
        return jsonify({"success": False, "error": message})
    except Exception as e:
        print(f"Refresh error: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@legacy_api_bp.route("/bigquery/debug", methods=["GET"])
@login_required
def debug_bigquery_data():
    try:
        data, error = purchase_orders_service.get_purchase_order_summary(limit=1, offset=0)
        if error:
            return jsonify({"success": False, "error": error})

        if data and len(data) > 0:
            sample_row = data[0]
            return jsonify({
                "success": True,
                "sample_row": sample_row,
                "date_fields": {
                    "requested_date": sample_row.get("requested_date"),
                    "entered_date": sample_row.get("entered_date"),
                    "received_date": sample_row.get("received_date"),
                    "completed_date": sample_row.get("completed_date"),
                },
            })
        return jsonify({"success": False, "error": "No data returned from BigQuery"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@legacy_api_bp.route("/bigquery/cache-status", methods=["GET"])
@login_required
def cache_status():
    """Compatibility shim — see :func:`system_api.routes.cache_status`."""
    return _system_cache_status()


@legacy_api_bp.route("/bigquery/clear-cache", methods=["POST"])
@login_required
def clear_cache():
    """Compatibility shim — see :func:`system_api.routes.clear_cache`."""
    return _system_clear_cache()


@legacy_api_bp.route("/bigquery/start-refresh", methods=["POST"])
@login_required
def start_background_refresh():
    """Compatibility shim — see :func:`system_api.routes.start_background_refresh`."""
    return _system_start_refresh()
