"""Cache status / refresh / clear endpoints (system-level)."""
from __future__ import annotations

import threading
import time
from datetime import datetime

from flask import current_app, jsonify
from flask_login import login_required

from app.blueprints.system_api import system_api_bp
from app.extensions import db
from app.models.customer_cache import (
    CacheWatermark,
    CachedCallBehavior,
    CachedCallHistory,
    CachedEmailMessage,
    CachedNetoProduct,
    CachedPhoneLookup,
)
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.services.cache import cache_purchase_order_data
from app.services.sync_state import sync_state_service


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _max_cached_at(model) -> datetime | None:
    """Return the most recent cached_at across rows of a cache table."""
    row = db.session.query(model.cached_at).order_by(model.cached_at.desc()).first()
    return row[0] if row and row[0] else None


def _cache_status_payload() -> dict:
    summary_count = CachedPurchaseOrderSummary.query.count()
    items_count = CachedPurchaseOrderItem.query.count()
    comparison_count = CachedPurchaseOrderComparison.query.count()

    # Per-cache freshness — newest row in each table.
    po_at      = _max_cached_at(CachedPurchaseOrderSummary)
    pl_at      = _max_cached_at(CachedPhoneLookup)
    ch_at      = _max_cached_at(CachedCallHistory)
    cb_at      = _max_cached_at(CachedCallBehavior)
    np_at      = _max_cached_at(CachedNetoProduct)
    em_at      = _max_cached_at(CachedEmailMessage)

    # customer_360 is upserted incrementally — its cached_at on each
    # row only reflects when THAT row last changed, not the last sync
    # run. Use the dedicated watermark instead so a quiet hour doesn't
    # look stale.
    cust_wm = (db.session.query(CacheWatermark.last_synced_at)
               .filter_by(cache_name="customer_360").first())
    cust_at = cust_wm[0] if cust_wm else None

    # Headline freshness = OLDEST of the loaded caches. That's what
    # actually answers "is anything stale?" for an agent. Empty caches
    # are skipped so the indicator doesn't go red just because we
    # haven't loaded one yet.
    candidates = {
        "PO":            po_at,
        "phone_lookup":  pl_at,
        "call_history":  ch_at,
        "call_behavior": cb_at,
        "neto_product":  np_at,
        "customer_360":  cust_at,
        "email_archive": em_at,
    }
    loaded = {k: v for k, v in candidates.items() if v is not None}
    oldest_at = min(loaded.values()) if loaded else None
    oldest_name = (min(loaded, key=loaded.get)) if loaded else None

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
        # last_refresh_time is the OLDEST cache (worst-case freshness),
        # so the topbar indicator answers "is anything stale?"
        "last_refresh_time": _iso(oldest_at),
        "oldest_cache":      oldest_name,
        # last_cached kept for backward-compat with anything still
        # reading it — points at the PO cache like before.
        "last_cached":       _iso(po_at),
        "caches": {
            "po":            _iso(po_at),
            "phone_lookup":  _iso(pl_at),
            "call_history":  _iso(ch_at),
            "call_behavior": _iso(cb_at),
            "neto_product":  _iso(np_at),
            "customer_360":  _iso(cust_at),
            "email_archive": _iso(em_at),
        },
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


def _run_customer_cache_with_app_context(app):
    """Refresh the customer-360 + email-archive caches in a daemon thread.

    Runs the same set of loaders that ``flask refresh-cache`` does, minus
    the PO loader which has its own dedicated UI button.
    """
    from app.services.customer_cache import cache_customer_360_data
    from app.services.email_cache import cache_email_archive
    with app.app_context():
        cache_customer_360_data()
        cache_email_archive()


@system_api_bp.route("/start-refresh", methods=["POST"])
@login_required
def start_background_refresh():
    """Refresh the cache for the requested context.

    Body / query param ``cache`` selects what to refresh:
      * ``po``           — purchase-order cache (default, legacy behaviour)
      * ``customer_360`` — customer_360 + phone_lookup + call_history +
                           call_behavior + neto_product + email_archive
    """
    from flask import request
    cache_name = (request.args.get("cache")
                  or (request.get_json(silent=True) or {}).get("cache")
                  or "po")
    try:
        app = current_app._get_current_object()
        if cache_name == "customer_360":
            target = _run_customer_cache_with_app_context
            label = "Customer 360"
        else:
            target = _run_cache_with_app_context
            label = "Purchase Orders"
        thread = threading.Thread(target=target, args=(app,))
        thread.daemon = True
        thread.start()
        return jsonify({"success": True,
                        "message": f"Background refresh started ({label})"})
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"success": False, "message": f"Error starting refresh: {str(e)}"}), 500
