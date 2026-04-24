"""Cache status / refresh / clear endpoints (system-level)."""
from __future__ import annotations

import threading
import time

from flask import current_app, jsonify
from flask_login import login_required

from app.blueprints.system_api import system_api_bp
from app.extensions import db
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.services.cache import cache_purchase_order_data
from app.services.sync_state import sync_state_service


def _cache_status_payload() -> dict:
    summary_count = CachedPurchaseOrderSummary.query.count()
    items_count = CachedPurchaseOrderItem.query.count()
    comparison_count = CachedPurchaseOrderComparison.query.count()

    last_cached = None
    latest_record = (
        CachedPurchaseOrderSummary.query.order_by(CachedPurchaseOrderSummary.cached_at.desc()).first()
    )
    if latest_record and latest_record.cached_at:
        last_cached = latest_record.cached_at.isoformat() + "Z"

    snapshot = sync_state_service.snapshot()
    return {
        "success": True,
        "summary_count": summary_count,
        "items_count": items_count,
        "comparison_count": comparison_count,
        "summary_total": snapshot["summary_total"],
        "items_total": snapshot["items_total"],
        "comparison_total": snapshot["comparison_total"],
        "has_cached_data": summary_count > 0,
        "last_cached": last_cached,
        "last_refresh_time": last_cached,
        "is_syncing": snapshot["is_running"],
    }


@system_api_bp.route("/cache-status", methods=["GET"])
@login_required
def cache_status():
    try:
        return jsonify(_cache_status_payload())
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"success": False, "error": str(e)})


@system_api_bp.route("/clear-cache", methods=["POST"])
@login_required
def clear_cache():
    try:
        if sync_state_service.is_running:
            sync_state_service.request_stop()
            time.sleep(2)

        CachedPurchaseOrderSummary.query.delete()
        CachedPurchaseOrderItem.query.delete()
        CachedPurchaseOrderComparison.query.delete()
        db.session.commit()

        sync_state_service.finish()

        return jsonify({
            "success": True,
            "message": "Cache cleared. Any ongoing sync has been stopped.",
        })
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"success": False, "error": str(e)})


def _run_cache_with_app_context(app):
    with app.app_context():
        cache_purchase_order_data()


@system_api_bp.route("/start-refresh", methods=["POST"])
@login_required
def start_background_refresh():
    try:
        app = current_app._get_current_object()
        thread = threading.Thread(target=_run_cache_with_app_context, args=(app,))
        thread.daemon = True
        thread.start()
        return jsonify({"success": True, "message": "Background refresh started"})
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"success": False, "message": f"Error starting refresh: {str(e)}"}), 500
