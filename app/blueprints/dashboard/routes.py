"""Root index + dashboard pages."""
from __future__ import annotations

from flask import redirect, render_template, url_for
from flask_login import current_user, login_required

from app.blueprints.dashboard import dashboard_bp
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.models.reviews import OPEN_REVIEW_STATUSES, ItemReview
from app.models.user import LoginLog
from app.services.sync_state import sync_state_service


@dashboard_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))
    return redirect(url_for("auth.login"))


@dashboard_bp.route("/dashboard")
@login_required
def dashboard():
    summary_count = CachedPurchaseOrderSummary.query.count()
    items_count = CachedPurchaseOrderItem.query.count()
    comparison_count = CachedPurchaseOrderComparison.query.count()

    if summary_count == 0 and items_count == 0 and comparison_count == 0:
        return render_template("dashboard_progress.html")

    recent_logins = (
        LoginLog.query.filter_by(user_id=current_user.id)
        .order_by(LoginLog.login_time.desc())
        .limit(5)
        .all()
    )

    latest = (
        CachedPurchaseOrderSummary.query.order_by(CachedPurchaseOrderSummary.cached_at.desc()).first()
    )
    last_refresh = latest.cached_at.strftime("%Y-%m-%d %H:%M:%S") if latest and latest.cached_at else None
    open_reviews = ItemReview.query.filter(ItemReview.status.in_(OPEN_REVIEW_STATUSES)).count()
    kpis = {
        "summary_count": summary_count,
        "items_count": items_count,
        "comparison_count": comparison_count,
        "open_reviews": open_reviews,
        "last_refresh": last_refresh,
        "is_syncing": sync_state_service.is_running,
    }
    return render_template("dashboard_v2.html", recent_logins=recent_logins, kpis=kpis)


@dashboard_bp.route("/dashboard-progress")
@login_required
def dashboard_progress():
    """Progress page for cache refresh."""
    return render_template("dashboard_progress.html")
