"""MSL Changes page: list pending changes, bulk approve, show recent history."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.abilities import require_capability
from app.blueprints.validation import validation_bp
from app.services.msl_service import (
    get_pending_msl_changes,
    get_recent_decisions,
    record_decisions,
)


@validation_bp.route("/msl")
@login_required
@require_capability("validation.view")
def msl_changes():
    pending, pending_err = get_pending_msl_changes()
    history, history_err = get_recent_decisions(days=30, limit=200)

    if pending_err:
        flash(f"Could not load MSL changes: {pending_err}", "error")
    if history_err:
        flash(f"Could not load MSL decision history: {history_err}", "error")

    return render_template(
        "validation/msl_changes.html",
        pending=pending,
        history=history,
    )


@validation_bp.route("/msl/approve", methods=["POST"])
@login_required
@require_capability("validation.msl.approve")
def msl_approve():
    """Bulk-approve one or more rows.

    The form posts a ``keys`` field per selected checkbox, formatted as
    ``<manufacturer_sku>|<product_modified_on_iso>`` (see
    :meth:`MSLChange.row_key`). We split here, write one decision row per
    key, then redirect back to the queue.
    """
    raw_keys = request.form.getlist("keys")
    keys: list[tuple[str, str]] = []
    for raw in raw_keys:
        if "|" not in raw:
            continue
        sku, ts = raw.split("|", 1)
        sku = sku.strip()
        ts = ts.strip()
        if sku and ts:
            keys.append((sku, ts))

    if not keys:
        flash("No rows selected.", "error")
        return redirect(url_for("validation.msl_changes"))

    comment = (request.form.get("comment") or "").strip() or None
    inserted, errors = record_decisions(
        keys=keys,
        decision="approved",
        decided_by=current_user.username,
        comment=comment,
    )

    if inserted:
        flash(
            f"Approved {inserted} MSL change{'' if inserted == 1 else 's'}.",
            "success",
        )
    for err in errors:
        flash(err, "error")

    return redirect(url_for("validation.msl_changes"))
