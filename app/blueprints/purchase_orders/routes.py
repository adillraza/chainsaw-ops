"""Server-rendered PO Cross Check screens (v2 shell).

The pages are intentionally thin wrappers around the cached models so they
render fast and are easy to evolve. HTMX swaps in table fragments from the
``rows`` endpoints; full-page loads share the same query parameters so links
are bookmarkable.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.auth.abilities import require_capability
from app.blueprints.purchase_orders import purchase_orders_bp
from app.extensions import db
from app.models.annotations import Annotation
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.models.reviews import OPEN_REVIEW_STATUSES, ItemReview
from app.services.cache import update_cache_with_latest_note
from app.services.purchase_orders_service import purchase_orders_service
from app.services.reviews_sync import sync_review_to_bigquery


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_htmx() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _paginate(query, page: int, per_page: int):
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    total = query.count()
    rows = query.limit(per_page).offset((page - 1) * per_page).all()
    pages = max(1, (total + per_page - 1) // per_page)
    return rows, {"page": page, "per_page": per_page, "total": total, "pages": pages}


def _summary_query(search: str | None, supplier: str | None, status: str | None):
    q = CachedPurchaseOrderSummary.query
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                CachedPurchaseOrderSummary.po_id.ilike(like),
                CachedPurchaseOrderSummary.order_id.ilike(like),
            )
        )
    if supplier:
        q = q.filter(CachedPurchaseOrderSummary.supplier == supplier)
    if status:
        q = q.filter(CachedPurchaseOrderSummary.po_status == status)
    return q.order_by(db.cast(CachedPurchaseOrderSummary.po_id, db.Integer).desc())


def _supplier_options() -> list[str]:
    rows = (
        db.session.query(CachedPurchaseOrderSummary.supplier)
        .filter(CachedPurchaseOrderSummary.supplier.isnot(None))
        .distinct()
        .order_by(CachedPurchaseOrderSummary.supplier)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def _status_options() -> list[str]:
    rows = (
        db.session.query(CachedPurchaseOrderSummary.po_status)
        .filter(CachedPurchaseOrderSummary.po_status.isnot(None))
        .distinct()
        .order_by(CachedPurchaseOrderSummary.po_status)
        .all()
    )
    return [r[0] for r in rows if r[0]]


# ---------------------------------------------------------------------------
# REX PO Orders – list + detail
# ---------------------------------------------------------------------------

@purchase_orders_bp.route("/orders")
@login_required
def orders():
    search = request.args.get("q", "").strip() or None
    supplier = request.args.get("supplier") or None
    status = request.args.get("status") or None
    page = int(request.args.get("page", 1) or 1)
    per_page = int(request.args.get("per_page", 50) or 50)

    rows, meta = _paginate(_summary_query(search, supplier, status), page, per_page)

    template = "purchase_orders/_partials/orders_table.html" if _is_htmx() else "purchase_orders/orders.html"
    return render_template(
        template,
        rows=rows,
        meta=meta,
        filters={"q": search or "", "supplier": supplier or "", "status": status or ""},
        suppliers=_supplier_options(),
        statuses=_status_options(),
    )


@purchase_orders_bp.route("/orders/<po_id>")
@login_required
def order_detail(po_id: str):
    summary = CachedPurchaseOrderSummary.query.filter_by(po_id=str(po_id)).first()
    if not summary:
        abort(404)

    items = (
        CachedPurchaseOrderItem.query.filter_by(po_id=str(po_id))
        .order_by(CachedPurchaseOrderItem.po_item_id)
        .all()
    )
    comparisons = (
        CachedPurchaseOrderComparison.query.filter_by(po_id=str(po_id))
        .order_by(CachedPurchaseOrderComparison.sku)
        .all()
    )
    reviews = (
        ItemReview.query.filter_by(po_id=str(po_id))
        .order_by(ItemReview.flagged_at.desc())
        .all()
    )
    annotations = (
        Annotation.query.filter_by(entity_type="purchase_order", entity_id=str(po_id))
        .order_by(Annotation.created_at.desc())
        .all()
    )

    open_review_count = sum(1 for r in reviews if r.status in OPEN_REVIEW_STATUSES)
    item_disparities = sum(1 for i in items if i.disparity)
    items_with_notes = sum(
        1 for c in comparisons
        if c.latest_item_note and c.latest_item_note.strip()
    )
    # Anything in change_log that isn't blank or 'No Change' counts as an issue
    change_issues = sum(
        1 for c in comparisons
        if c.change_log and c.change_log != "No Change"
    )
    # Kitted items shouldn't appear on a PO at all — surface as a separate KPI
    # tile so the warning is visible before the user opens the comparison view.
    kitted_count = sum(1 for c in comparisons if c.is_kitted_item)
    last_note_dt = max(
        (c.latest_item_note_date for c in comparisons if c.latest_item_note_date),
        default=None,
    )
    annotation_dt = max(
        (a.created_at for a in annotations if a.created_at),
        default=None,
    )
    last_activity = max(
        (d for d in (last_note_dt, annotation_dt) if d),
        default=None,
    )

    return render_template(
        "purchase_orders/order_detail.html",
        summary=summary,
        items=items,
        comparisons=comparisons,
        reviews=reviews,
        annotations=annotations,
        kpis={
            # NOTE: avoid the key name "items" because Jinja resolves
            # ``kpis.items`` to dict.items (a method) instead of the value.
            "item_count": len(items),
            "items_with_notes": items_with_notes,
            "change_issues": change_issues,
            "open_reviews": open_review_count,
            "item_disparities": item_disparities,
            "kitted_count": kitted_count,
            "last_activity": last_activity,
        },
    )


# ---------------------------------------------------------------------------
# Comparison – item-level drill into a PO (or across all POs)
# ---------------------------------------------------------------------------

def _comparison_base_query(
    po_id: str | None,
    search: str | None,
    sku: str | None,
):
    q = CachedPurchaseOrderComparison.query
    if po_id:
        q = q.filter(CachedPurchaseOrderComparison.po_id == str(po_id))
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                CachedPurchaseOrderComparison.po_id.ilike(like),
                CachedPurchaseOrderComparison.sku.ilike(like),
                CachedPurchaseOrderComparison.name.ilike(like),
            )
        )
    if sku:
        like = f"%{sku}%"
        q = q.filter(CachedPurchaseOrderComparison.sku.ilike(like))
    return q


def _po_context(po_id: str) -> dict[str, Any] | None:
    summary = CachedPurchaseOrderSummary.query.filter_by(po_id=str(po_id)).first()
    if not summary:
        return None
    open_reviews = (
        ItemReview.query.filter(
            ItemReview.po_id == str(po_id),
            ItemReview.status.in_(OPEN_REVIEW_STATUSES),
        )
        .count()
    )
    return {"summary": summary, "open_reviews": open_reviews}


def _open_reviews_by_item(po_id: str) -> dict[str, ItemReview]:
    rows = (
        ItemReview.query.filter(
            ItemReview.po_id == str(po_id),
            ItemReview.status.in_(OPEN_REVIEW_STATUSES),
        )
        .all()
    )
    return {str(r.po_item_id): r for r in rows if r.po_item_id}


def _backfill_po_metadata_into_cache(rows, po_id):
    """Opportunistically refresh `po_item_id` + `latest_item_note*` for a PO.

    Older cache entries (pre-fix) were written without `po_item_id` or
    latest-note fields, so notes never appeared in the comparison view even
    though the data exists in BigQuery. When the user opens a single PO we do
    one cheap BigQuery call (`get_purchase_order_comparison(po_id=...)`) which
    already returns both `po_item_id` and the joined latest note per row.
    We then merge those values back into the cached SQLAlchemy rows by SKU
    so the user sees fresh data immediately, and subsequent requests read
    straight from cache.

    The merge is best-effort — any failure is swallowed so the page still
    renders from cache.
    """
    if not po_id or not rows:
        return

    # Trigger only when po_item_id is missing — that's the unambiguous signal
    # that the cache row pre-dates the schema fix. Once po_item_ids are filled,
    # subsequent note inserts/edits update the cache via update_cache_with_latest_note,
    # and the next bulk sync will refresh notes via the BigQuery JOIN.
    if not any(r.po_item_id is None for r in rows):
        return

    fresh, err = purchase_orders_service.get_purchase_order_comparison(po_id=po_id)
    if err or not fresh:
        return

    # Build SKU -> fresh-row map. If a SKU appears multiple times we keep the
    # entry that has a po_item_id (preferred) or a note attached.
    by_sku: dict[str, dict[str, Any]] = {}
    for f in fresh:
        sku = f.get("sku")
        if not sku:
            continue
        existing = by_sku.get(sku)
        if existing is None:
            by_sku[sku] = f
            continue
        existing_score = (1 if existing.get("po_item_id") else 0) + (1 if existing.get("latest_item_note") else 0)
        new_score = (1 if f.get("po_item_id") else 0) + (1 if f.get("latest_item_note") else 0)
        if new_score > existing_score:
            by_sku[sku] = f

    from datetime import datetime
    dirty = False
    for r in rows:
        match = by_sku.get(r.sku) if r.sku else None
        if not match:
            continue
        if r.po_item_id is None and match.get("po_item_id"):
            r.po_item_id = str(match["po_item_id"])
            dirty = True
        comment = match.get("latest_item_note")
        if (r.latest_item_note or "") != (comment or ""):
            r.latest_item_note = comment
            r.latest_item_note_user = match.get("latest_item_note_user")
            created = match.get("latest_item_note_date")
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    created = None
            r.latest_item_note_date = created
            dirty = True

    if dirty:
        try:
            db.session.commit()
        except Exception as exc:  # pragma: no cover - cache write failures shouldn't break UI
            db.session.rollback()
            print(f"Warning: failed to backfill metadata into cache for PO {po_id}: {exc}")


def _backfill_neto_product_ids(rows):
    """Opportunistically populate `neto_product_id` on cached item rows.

    The bulk sync now LEFT JOINs ``dataform.neto_product_list`` so future cache
    rebuilds will have ``neto_product_id`` populated. For rows already in the
    cache prior to that change, look up the missing IDs in a single BigQuery
    call (one ``IN`` query keyed on SKU) and write them back so the SKU column
    can render the Neto deep link immediately. Best-effort — failures are
    swallowed so the page still renders from cache.
    """
    if not rows:
        return

    pending = [r for r in rows if r.sku and not r.neto_product_id]
    if not pending:
        return

    # Dedupe SKUs to keep the BigQuery query cheap; cap to a sane upper bound
    # so a runaway page doesn't ship a million-element parameter array. The
    # unscoped comparison view can have thousands of SKUs, but neto_product_list
    # is a small dim table so the lookup is still cheap at this size.
    skus = {r.sku for r in pending}
    if not skus or len(skus) > 10000:
        return

    client = purchase_orders_service.client
    if client is None:
        return

    try:
        from google.cloud import bigquery  # local import keeps top of file clean

        query = f"""
        SELECT SKU, CAST(ID AS STRING) AS neto_product_id
        FROM `{purchase_orders_service.project_id}.dataform.neto_product_list`
        WHERE SKU IN UNNEST(@skus)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("skus", "STRING", list(skus)),
            ]
        )
        results = list(client.query(query, job_config=job_config).result())
    except Exception as exc:  # pragma: no cover - network/auth issues shouldn't break UI
        print(f"Warning: neto_product_id backfill query failed: {exc}")
        return

    by_sku = {row.SKU: row.neto_product_id for row in results if row.SKU}
    if not by_sku:
        return

    dirty = False
    for r in pending:
        pid = by_sku.get(r.sku)
        if pid and r.neto_product_id != pid:
            r.neto_product_id = pid
            dirty = True

    if dirty:
        try:
            db.session.commit()
        except Exception as exc:  # pragma: no cover
            db.session.rollback()
            print(f"Warning: failed to write neto_product_id backfill to cache: {exc}")


def _backfill_is_kitted_item(rows):
    """Opportunistically populate ``is_kitted_item`` on cached rows.

    The bulk sync now projects ``is_kitted_item`` straight from BigQuery, but
    rows cached prior to that change are NULL. ``is_kitted_item`` is a
    product-level attribute (a SKU is either a kit or it isn't), so a single
    SKU-keyed lookup against ``neto_rex_purchase_order_report`` is sufficient
    regardless of which PO the row belongs to. Works for both
    ``CachedPurchaseOrderItem`` and ``CachedPurchaseOrderComparison`` since
    both expose ``sku`` and ``is_kitted_item``. Best-effort — failures are
    swallowed so the page still renders from cache.
    """
    if not rows:
        return

    pending = [r for r in rows if r.sku and r.is_kitted_item is None]
    if not pending:
        return

    skus = {r.sku for r in pending}
    if not skus or len(skus) > 10000:
        return

    client = purchase_orders_service.client
    if client is None:
        return

    try:
        from google.cloud import bigquery  # local import keeps top of file clean

        # ANY_VALUE because the same SKU can appear on many POs; the kit flag
        # is a property of the product so any non-null value is fine.
        query = f"""
        SELECT manufacturer_sku AS sku, ANY_VALUE(is_kitted_item) AS is_kitted_item
        FROM `{purchase_orders_service.project_id}.dataform.neto_rex_purchase_order_report`
        WHERE manufacturer_sku IN UNNEST(@skus)
          AND is_kitted_item IS NOT NULL
        GROUP BY manufacturer_sku
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("skus", "STRING", list(skus)),
            ]
        )
        results = list(client.query(query, job_config=job_config).result())
    except Exception as exc:  # pragma: no cover - network/auth issues shouldn't break UI
        print(f"Warning: is_kitted_item backfill query failed: {exc}")
        return

    by_sku = {row.sku: bool(row.is_kitted_item) for row in results if row.sku is not None}
    if not by_sku:
        return

    dirty = False
    for r in pending:
        flag = by_sku.get(r.sku)
        if flag is not None and r.is_kitted_item != flag:
            r.is_kitted_item = flag
            dirty = True

    if dirty:
        try:
            db.session.commit()
        except Exception as exc:  # pragma: no cover
            db.session.rollback()
            print(f"Warning: failed to write is_kitted_item backfill to cache: {exc}")


# Columns the user can sort by from the table headers. Maps a stable key
# (used in URLs) to a tuple of (model column, human label, tooltip text).
COMPARISON_COLUMNS: dict[str, dict[str, Any]] = {
    "po_id": {
        "col": CachedPurchaseOrderComparison.po_id,
        "label": "PO ID",
        "tooltip": "Purchase Order ID",
        "align": "left",
    },
    "sku": {
        "col": CachedPurchaseOrderComparison.sku,
        "label": "SKU",
        "tooltip": "Stock Keeping Unit identifier",
        "align": "left",
    },
    "name": {
        "col": CachedPurchaseOrderComparison.name,
        "label": "Description",
        "tooltip": "Product name and description",
        "align": "left",
    },
    "change_log": {
        "col": CachedPurchaseOrderComparison.change_log,
        "label": "Change log",
        "tooltip": "Type of change or difference detected between REX and NETO",
        "align": "left",
    },
    "rex_available_qty": {
        "col": CachedPurchaseOrderComparison.rex_available_qty,
        "label": "REX avail",
        "tooltip": "Quantity currently available in REX",
        "align": "right",
    },
    "neto_qty_available": {
        "col": CachedPurchaseOrderComparison.neto_qty_available,
        "label": "NETO avail",
        "tooltip": "Quantity currently available in NETO",
        "align": "right",
    },
    "original_rex_qty_ordered": {
        "col": CachedPurchaseOrderComparison.original_rex_qty_ordered,
        "label": "Original",
        "tooltip": "Original quantity ordered in REX",
        "align": "right",
    },
    "final_rex_qty_ordered": {
        "col": CachedPurchaseOrderComparison.final_rex_qty_ordered,
        "label": "Latest",
        "tooltip": "Final / latest quantity ordered in REX",
        "align": "right",
    },
    "neto_qty_shipped": {
        "col": CachedPurchaseOrderComparison.neto_qty_shipped,
        "label": "Shipped",
        "tooltip": "Quantity shipped from NETO",
        "align": "right",
    },
    "rex_qty_received": {
        "col": CachedPurchaseOrderComparison.rex_qty_received,
        "label": "Received",
        "tooltip": "Quantity received in REX",
        "align": "right",
    },
    "latest_item_note_date": {
        "col": CachedPurchaseOrderComparison.latest_item_note_date,
        "label": "Latest note",
        "tooltip": "Most recent item-level note or comment",
        "align": "left",
    },
}

DEFAULT_SORT_KEY = "change_log"
DEFAULT_SORT_DIR = "asc"
COMPARISON_ROW_CAP = 5000


@purchase_orders_bp.route("/comparison")
@login_required
def comparison():
    search = request.args.get("q", "").strip() or None
    sku = request.args.get("sku", "").strip() or None
    po_id = request.args.get("po_id", "").strip() or None
    selected_changes = [c for c in request.args.getlist("change_type") if c]
    # Optional filter (driven by the PO Stats "Kitted items" KPI tile and any
    # other surface that wants to deep-link straight to kit anomalies). No
    # visible toolbar pill — when active we render a small "Showing kitted
    # items only" chip with a clear button so the user can see/dismiss it.
    kitted_only = request.args.get("kitted") == "1"
    sort_key = request.args.get("sort") or DEFAULT_SORT_KEY
    sort_dir = (request.args.get("dir") or DEFAULT_SORT_DIR).lower()
    if sort_key not in COMPARISON_COLUMNS:
        sort_key = DEFAULT_SORT_KEY
    if sort_dir not in ("asc", "desc"):
        sort_dir = DEFAULT_SORT_DIR

    base_q = _comparison_base_query(po_id, search, sku)

    # Pill counts are computed within the current scope (po_id + search + sku),
    # ignoring `selected_changes` so the user can see what each toggle would add.
    counts_rows = (
        base_q.with_entities(
            CachedPurchaseOrderComparison.change_log,
            db.func.count(CachedPurchaseOrderComparison.id),
        )
        .group_by(CachedPurchaseOrderComparison.change_log)
        .all()
    )
    change_type_counts: list[dict[str, Any]] = []
    total_in_scope = 0
    for change_log, count in counts_rows:
        total_in_scope += count
        if not change_log:
            continue
        change_type_counts.append({"code": change_log, "count": count})
    change_type_counts.sort(key=lambda r: (-r["count"], r["code"]))

    q = base_q
    if selected_changes:
        q = q.filter(CachedPurchaseOrderComparison.change_log.in_(selected_changes))
    if kitted_only:
        q = q.filter(CachedPurchaseOrderComparison.is_kitted_item.is_(True))

    # Hard priority groups (applied before the user's chosen sort):
    #   0 = items with a latest note            -> always pinned to the top
    #   1 = BO / IR Receival Error              -> loud operational errors
    #   2 = regular change-log values
    #   3 = Back-ordered                        -> second-to-last bucket
    #   4 = No Change                           -> always at the bottom
    # Within each bucket, the user's selected sort + SKU tiebreaker apply.
    note_present = db.func.coalesce(
        db.func.length(db.func.trim(CachedPurchaseOrderComparison.latest_item_note)),
        0,
    ) > 0
    priority = db.case(
        (note_present, 0),
        (
            CachedPurchaseOrderComparison.change_log.in_(
                ("BO Receival Error", "IR Receival Error")
            ),
            1,
        ),
        (CachedPurchaseOrderComparison.change_log == "No Change", 4),
        (CachedPurchaseOrderComparison.change_log == "Back-ordered", 3),
        else_=2,
    )
    sort_col = COMPARISON_COLUMNS[sort_key]["col"]
    primary = sort_col.desc() if sort_dir == "desc" else sort_col.asc()
    q = q.order_by(priority.asc(), primary, CachedPurchaseOrderComparison.sku.asc())

    rows = q.limit(COMPARISON_ROW_CAP).all()
    truncated = len(rows) >= COMPARISON_ROW_CAP

    # When the user is looking at a single PO, refresh po_item_id +
    # latest-note columns directly from BigQuery so cache rows that pre-date
    # the cache schema fix still show the real note immediately.
    if po_id:
        _backfill_po_metadata_into_cache(rows, po_id)

    # Same one-shot Neto product ID backfill the cost-prices view uses, so the
    # SKU column on the comparison page deep-links into Neto cpanel even for
    # rows cached before the BigQuery JOIN was added.
    _backfill_neto_product_ids(rows)
    _backfill_is_kitted_item(rows)

    po_ctx = _po_context(po_id) if po_id else None
    open_review_map = _open_reviews_by_item(po_id) if po_id else {}

    template = "purchase_orders/_partials/comparison_table.html" if _is_htmx() else "purchase_orders/comparison.html"
    return render_template(
        template,
        rows=rows,
        truncated=truncated,
        row_cap=COMPARISON_ROW_CAP,
        filters={
            "q": search or "",
            "sku": sku or "",
            "po_id": po_id or "",
            "change_type": selected_changes,
            "kitted": kitted_only,
            "sort": sort_key,
            "dir": sort_dir,
        },
        sort_key=sort_key,
        sort_dir=sort_dir,
        sort_columns=COMPARISON_COLUMNS,
        change_type_counts=change_type_counts,
        total_in_scope=total_in_scope,
        po_ctx=po_ctx,
        open_review_map=open_review_map,
    )


# ---------------------------------------------------------------------------
# Comparison row helpers (HTMX swaps for inline note + flag actions)
# ---------------------------------------------------------------------------

def _render_comparison_row(row_id: int):
    row = CachedPurchaseOrderComparison.query.get_or_404(row_id)
    open_review_map = _open_reviews_by_item(row.po_id) if row.po_id else {}
    return render_template(
        "purchase_orders/_partials/comparison_row.html",
        r=row,
        open_review_map=open_review_map,
        po_ctx={"summary": CachedPurchaseOrderSummary.query.filter_by(po_id=str(row.po_id)).first()} if row.po_id else None,
    )


@purchase_orders_bp.route("/comparison/row/<int:row_id>")
@login_required
def comparison_row(row_id: int):
    return _render_comparison_row(row_id)


@purchase_orders_bp.route("/comparison/row/<int:row_id>/note-form")
@login_required
def comparison_row_note_form(row_id: int):
    row = CachedPurchaseOrderComparison.query.get_or_404(row_id)
    notes, _err = purchase_orders_service.get_item_notes(row.po_item_id) if row.po_item_id else ([], None)
    return render_template(
        "purchase_orders/_partials/comparison_note_panel.html",
        r=row,
        notes=notes or [],
    )


@purchase_orders_bp.route("/comparison/row/<int:row_id>/note", methods=["POST"])
@login_required
def comparison_row_save_note(row_id: int):
    row = CachedPurchaseOrderComparison.query.get_or_404(row_id)
    if not row.po_item_id:
        return ("This row has no po_item_id and cannot accept notes.", 400)

    comment = (request.form.get("comment") or "").strip()
    if not comment:
        return ("Comment is required.", 400)

    success, message = purchase_orders_service.save_item_note(
        po_item_id=row.po_item_id,
        po_id=row.po_id,
        sku=row.sku,
        comment=comment,
        username=current_user.username,
    )
    if not success:
        return (f"Failed to save note: {message}", 500)

    try:
        update_cache_with_latest_note(row.po_item_id, row.po_id)
        db.session.refresh(row)
    except Exception as exc:  # pragma: no cover - cache failures shouldn't block UI
        print(f"Warning: cache refresh after note save failed: {exc}")

    return _render_comparison_row(row.id)


def _neto_product_url(neto_product_id) -> str | None:
    if not neto_product_id:
        return None
    return f"https://www.chainsawspares.com.au/_cpanel/products/view?id={neto_product_id}"


@purchase_orders_bp.route("/comparison/row/<int:row_id>/flag-drawer")
@login_required
@require_capability("reviews.flag")
def comparison_row_flag_drawer(row_id: int):
    """Render the flag drawer body for a single comparison row.

    If the item already has an open review, we swap in the detail drawer so
    the user operates on the existing review rather than hitting the dedupe
    guard on POST /flag.
    """
    row = CachedPurchaseOrderComparison.query.get_or_404(row_id)
    duplicate = None
    if row.po_item_id:
        duplicate = ItemReview.query.filter(
            ItemReview.po_id == row.po_id,
            ItemReview.po_item_id == str(row.po_item_id),
            ItemReview.status.in_(OPEN_REVIEW_STATUSES),
        ).first()

    return render_template(
        "purchase_orders/_partials/review_drawer_flag.html",
        r=row,
        duplicate=duplicate,
        neto_url=_neto_product_url(row.neto_product_id),
    )


@purchase_orders_bp.route("/comparison/row/<int:row_id>/flag", methods=["POST"])
@login_required
@require_capability("reviews.flag")
def comparison_row_flag(row_id: int):
    row = CachedPurchaseOrderComparison.query.get_or_404(row_id)
    if not row.po_item_id:
        return ("This row has no po_item_id and cannot be flagged.", 400)

    existing = ItemReview.query.filter(
        ItemReview.po_id == row.po_id,
        ItemReview.po_item_id == str(row.po_item_id),
        ItemReview.status.in_(OPEN_REVIEW_STATUSES),
    ).first()
    if existing:
        # Someone beat us to it (or a stale drawer); close the drawer and
        # just re-render the row so the "Flagged" badge reflects reality.
        return _render_comparison_row(row.id), 200, {"HX-Trigger": "closeReviewDrawer"}

    # Snapshot captures the comparison numbers *and* the latest item-note at
    # flag time, so the warehouse detail drawer can show what retail saw even
    # if the BigQuery note changes later.
    snapshot = {
        "change_log": row.change_log,
        "short_description": row.name,
        "rex_available_qty": row.rex_available_qty,
        "neto_qty_available": row.neto_qty_available,
        "original_rex_qty_ordered": row.original_rex_qty_ordered,
        "final_rex_qty_ordered": row.final_rex_qty_ordered,
        "neto_qty_shipped": row.neto_qty_shipped,
        "rex_qty_received": row.rex_qty_received,
        "latest_item_note": row.latest_item_note,
        "latest_item_note_user": row.latest_item_note_user,
        "latest_item_note_date": row.latest_item_note_date.isoformat()
            if row.latest_item_note_date else None,
    }
    review = ItemReview(
        review_id=uuid.uuid4().hex,
        po_id=row.po_id,
        order_id=row.order_id,
        po_item_id=str(row.po_item_id),
        sku=row.sku,
        flagged_by=current_user.username,
        flag_comment=(request.form.get("comment") or "").strip() or None,
        comparison_snapshot=json.dumps(snapshot),
    )
    db.session.add(review)
    db.session.commit()

    try:
        sync_review_to_bigquery(review)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: failed to sync review to BigQuery: {exc}")

    response = _render_comparison_row(row.id)
    # Tuple short-circuits the response helper; wrap into a Response so we can
    # attach the HX-Trigger header cleanly.
    from flask import make_response

    resp = make_response(response)
    resp.headers["HX-Trigger"] = "closeReviewDrawer"
    return resp


# ---------------------------------------------------------------------------
# Review drawer + stage transitions (warehouse-complete / retail-complete / cancel)
# ---------------------------------------------------------------------------

def _pick_review_mode(review: ItemReview) -> str:
    """Decide which drawer mode to render based on role + status.

    - warehouse_respond: warehouse or admin acting on an open review.
    - retail_close:      retail or admin acting on a warehouse-completed review.
    - readonly:          everyone else (already-closed reviews, wrong role, etc.).
    """
    role = getattr(current_user, "role", None) or ""
    status = review.status or ""
    if status in OPEN_REVIEW_STATUSES and role in ("admin", "warehouse"):
        return "warehouse_respond"
    if status == "warehouse_closed" and role in ("admin", "retail"):
        return "retail_close"
    return "readonly"


def _render_review_drawer(review: ItemReview):
    """Render the shared review-detail drawer body for the given review."""
    snapshot: dict = {}
    if review.comparison_snapshot:
        try:
            snapshot = json.loads(review.comparison_snapshot) or {}
        except (TypeError, ValueError):
            snapshot = {}

    # Best-effort live row lookup so freshly changed quantities / notes
    # replace the frozen snapshot when available.
    current_row = None
    if review.po_item_id:
        current_row = CachedPurchaseOrderComparison.query.filter_by(
            po_id=review.po_id, po_item_id=review.po_item_id
        ).first()

    neto_product_id = getattr(current_row, "neto_product_id", None)
    return render_template(
        "purchase_orders/_partials/review_drawer_detail.html",
        review=review,
        mode=_pick_review_mode(review),
        snapshot=snapshot,
        current_row=current_row,
        neto_url=_neto_product_url(neto_product_id),
    )


def _review_transition_response(review: ItemReview):
    """Build an HTMX response after a stage transition.

    Re-renders the drawer body (so the user sees the new state) and asks
    the queue page to refresh via a custom trigger.
    """
    from flask import make_response

    resp = make_response(_render_review_drawer(review))
    resp.headers["HX-Trigger"] = "reviewUpdated"
    return resp


@purchase_orders_bp.route("/reviews/<review_id>/drawer")
@login_required
def review_drawer(review_id: str):
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    return _render_review_drawer(review)


@purchase_orders_bp.route("/reviews/<review_id>/warehouse-complete", methods=["POST"])
@login_required
@require_capability("reviews.warehouse.close")
def review_warehouse_complete(review_id: str):
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    if review.status not in OPEN_REVIEW_STATUSES:
        # Stale drawer — re-render so the user sees the real state.
        return _render_review_drawer(review)

    comment = (request.form.get("comment") or "").strip()
    if not comment:
        return ("Warehouse comment is required.", 400)

    now = datetime.utcnow()
    review.warehouse_comment = comment
    review.warehouse_assigned_to = current_user.username
    if not review.warehouse_started_at:
        review.warehouse_started_at = now
    review.warehouse_closed_at = now
    review.status = "warehouse_closed"
    review.updated_at = now
    db.session.commit()

    try:
        sync_review_to_bigquery(review)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: failed to sync review to BigQuery: {exc}")

    return _review_transition_response(review)


@purchase_orders_bp.route("/reviews/<review_id>/retail-complete", methods=["POST"])
@login_required
@require_capability("reviews.retail.close")
def review_retail_complete(review_id: str):
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    if review.status != "warehouse_closed":
        # Stale drawer — re-render so the user sees the real state.
        return _render_review_drawer(review)

    comment = (request.form.get("comment") or "").strip()
    now = datetime.utcnow()
    review.retail_comment = comment or None
    review.retail_closed_by = current_user.username
    review.retail_closed_at = now
    review.status = "retail_closed"
    review.updated_at = now
    db.session.commit()

    try:
        sync_review_to_bigquery(review)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: failed to sync review to BigQuery: {exc}")

    return _review_transition_response(review)


@purchase_orders_bp.route("/reviews/<review_id>/cancel", methods=["POST"])
@login_required
@require_capability("reviews.cancel")
def review_cancel(review_id: str):
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    if review.status in ("retail_closed", "cancelled"):
        return _render_review_drawer(review)

    review.status = "cancelled"
    review.updated_at = datetime.utcnow()
    db.session.commit()

    try:
        sync_review_to_bigquery(review)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: failed to sync review to BigQuery: {exc}")

    return _review_transition_response(review)


# ---------------------------------------------------------------------------
# Cost price check – items where neto vs rex disagrees
# ---------------------------------------------------------------------------

@purchase_orders_bp.route("/cost-prices")
@login_required
def cost_prices():
    search = request.args.get("q", "").strip() or None
    po_id = request.args.get("po_id", "").strip() or None
    only_disparity = request.args.get("only_disparity", "1") == "1"
    page = int(request.args.get("page", 1) or 1)
    per_page = int(request.args.get("per_page", 50) or 50)

    q = CachedPurchaseOrderItem.query
    if only_disparity:
        q = q.filter(CachedPurchaseOrderItem.disparity.is_(True))
    if po_id:
        # Exact PO match — used by the "Disparity" badge on the orders table
        # to drill into a single PO without matching POs that share a substring.
        q = q.filter(CachedPurchaseOrderItem.po_id == po_id)
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                CachedPurchaseOrderItem.po_id.ilike(like),
                CachedPurchaseOrderItem.sku.ilike(like),
                CachedPurchaseOrderItem.manufacturer_sku.ilike(like),
                CachedPurchaseOrderItem.short_description.ilike(like),
            )
        )
    q = q.order_by(db.func.abs(CachedPurchaseOrderItem.difference).desc().nullslast())
    rows, meta = _paginate(q, page, per_page)

    # Older cache rows pre-date the neto_product_id JOIN. Backfill on demand
    # so the SKU column can deep-link into the Neto cpanel without waiting for
    # the next bulk sync.
    _backfill_neto_product_ids(rows)
    _backfill_is_kitted_item(rows)

    template = "purchase_orders/_partials/cost_prices_table.html" if _is_htmx() else "purchase_orders/cost_prices.html"
    return render_template(
        template,
        rows=rows,
        meta=meta,
        filters={
            "q": search or "",
            "po_id": po_id or "",
            "only_disparity": "1" if only_disparity else "0",
        },
    )


# ---------------------------------------------------------------------------
# Retail / Warehouse review queues
# ---------------------------------------------------------------------------

def _require_review_capability(capability: str) -> None:
    """Raise 401/403 unless the current user holds ``capability``.

    Thin helper retained for the retail/warehouse queue handlers which mix
    server-render + HTMX partials and want to 401 on anonymous vs 403 on
    authenticated-but-denied.
    """
    if not current_user.is_authenticated:
        abort(401)
    if not current_user.can(capability):
        abort(403)


def _review_base_query(search: str | None):
    """Base query used by both queue views, already ordered newest-first."""
    q = ItemReview.query
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                ItemReview.po_id.ilike(like),
                ItemReview.sku.ilike(like),
                ItemReview.flag_comment.ilike(like),
                ItemReview.warehouse_comment.ilike(like),
                ItemReview.retail_comment.ilike(like),
            )
        )
    return q.order_by(ItemReview.flagged_at.desc())


@purchase_orders_bp.route("/retail")
@login_required
def retail():
    """Retail review queue split into three stage-based sections.

    - warehouse_completed: action queue (retail needs to close).
    - pending_warehouse:   informational (awaiting warehouse).
    - completed:           history (retail_closed or cancelled).
    """
    _require_review_capability("reviews.retail.view")
    search = request.args.get("q", "").strip() or None
    base = _review_base_query(search)

    warehouse_completed = base.filter(ItemReview.status == "warehouse_closed").all()
    pending_warehouse = base.filter(ItemReview.status.in_(OPEN_REVIEW_STATUSES)).all()
    completed = base.filter(ItemReview.status.in_(("retail_closed", "cancelled"))).limit(200).all()

    template = "purchase_orders/_partials/retail_queue_sections.html" if _is_htmx() else "purchase_orders/retail.html"
    return render_template(
        template,
        team="retail",
        filters={"q": search or ""},
        warehouse_completed=warehouse_completed,
        pending_warehouse=pending_warehouse,
        completed=completed,
    )


@purchase_orders_bp.route("/warehouse")
@login_required
def warehouse():
    """Warehouse review queue split into Open + Closed sections."""
    _require_review_capability("reviews.warehouse.view")
    search = request.args.get("q", "").strip() or None
    base = _review_base_query(search)

    open_reviews = base.filter(ItemReview.status.in_(OPEN_REVIEW_STATUSES)).all()
    closed_reviews = (
        base.filter(ItemReview.status.in_(("warehouse_closed", "retail_closed", "cancelled")))
        .limit(200)
        .all()
    )

    template = "purchase_orders/_partials/warehouse_queue_sections.html" if _is_htmx() else "purchase_orders/warehouse.html"
    return render_template(
        template,
        team="warehouse",
        filters={"q": search or ""},
        open_reviews=open_reviews,
        closed_reviews=closed_reviews,
    )


# ---------------------------------------------------------------------------
# Change log guide (static reference page)
# ---------------------------------------------------------------------------

# ``CHANGE_LOG_GUIDE_STAGES`` is the authoritative reference for the
# ``change_type`` values produced by ``dataform.neto_rex_purchase_order_compared``.
# Each stage groups change types by the point in the order lifecycle at which
# the rule fires; the entries are rendered as educational cards on the
# Change Log Guide page (see ``changelog_guide.html``).
#
# Sample rows use the shape (rex_qty_ordered, neto_qty_invoiced, rex_qty_received).
# Use ``None`` for "n/a / not yet known" so the template renders an em-dash.
CHANGE_LOG_GUIDE_STAGES: list[dict[str, Any]] = [
    {
        "title": "Before NETO has invoiced",
        "subtitle": "REX has the PO, NETO has not produced an invoice yet.",
        "icon": "fa-hourglass-half",
        "color": "amber",
        "entries": [
            {
                "code": "Pending Invoice",
                "condition": "REX has the PO but NETO has no completion date yet (and the order line is not skipped or back-ordered).",
                "description": (
                    "The order is still in flight. The warehouse hasn't dispatched / invoiced the SKU yet, "
                    "so we have no NETO numbers to compare against. No action required — wait for NETO to invoice."
                ),
                "samples": [(5, None, None)],
                "result": (
                    "5 units ordered in REX. NETO hasn't produced an invoice for this SKU yet — "
                    "the row will reclassify automatically once the warehouse completes the order."
                ),
            },
        ],
    },
    {
        "title": "After NETO dispatch but before REX receival",
        "subtitle": "NETO has invoiced; the goods are in transit or awaiting REX receival.",
        "icon": "fa-truck-fast",
        "color": "sky",
        "entries": [
            {
                "code": "Quantity Changed",
                "condition": "REX Qty Ordered ≠ NETO qty invoiced.",
                "description": (
                    "The warehouse invoiced a different quantity than was originally ordered. "
                    "Add a note explaining the change in quantity."
                ),
                "samples": [(5, 3, None)],
                "result": (
                    "5 units were ordered in REX but only 3 were invoiced in NETO. "
                    "Could be a shortage of stock, the warehouse being unable to fulfill the full quantity, "
                    "or partial availability. The warehouse has reduced the order quantity."
                ),
            },
            {
                "code": "Item Removed",
                "condition": "Item ordered in REX but not present on the NETO invoice (and not back-ordered).",
                "description": (
                    "The line was completely removed from the invoice. "
                    "Usually indicates the item was out of stock or unavailable. "
                    "Add a note explaining the removal."
                ),
                "samples": [(4, None, None)],
                "result": (
                    "4 units were ordered in REX but the item doesn't appear on the NETO invoice at all. "
                    "The warehouse has completely removed this item from the order, "
                    "likely due to stock unavailability or discontinuation."
                ),
                "note": (
                    "Also fires when the warehouse skips a SKU on an otherwise-completed NETO order "
                    "(the parent order has a completion date but this line item is missing)."
                ),
            },
            {
                "code": "Back-ordered",
                "condition": "NETO qty invoiced = 0 AND the SKU appears on the NETO backorder list.",
                "description": (
                    "The line was ordered but is currently out of stock with the warehouse. "
                    "It has been placed on backorder and will be shipped when stock becomes available. "
                    "Add a note indicating the item is back-ordered."
                ),
                "samples": [(5, 0, None)],
                "result": (
                    "5 units were ordered in REX, the invoice shows 0 quantity, "
                    "and the item appears on the NETO backorder list. "
                    "The warehouse is temporarily out of stock and will fulfill this order once inventory is replenished."
                ),
            },
            {
                "code": "New Item",
                "condition": "REX Qty Ordered = 0 AND NETO qty invoiced > 0.",
                "description": (
                    "A new line has been added to the invoice that wasn't part of the original order. "
                    "The warehouse may have substituted or added this item. Review and add a note if needed."
                ),
                "samples": [(0, 2, None)],
                "result": (
                    "0 units were ordered in REX but 2 units appear on the NETO invoice. "
                    "The warehouse has added this item to the order — possible mismatch between NETO and REX. "
                    "Report to management if this looks unusual."
                ),
            },
            {
                "code": "No Change",
                "condition": "REX Qty Ordered = NETO qty invoiced.",
                "description": (
                    "The ordered quantity matches the invoiced quantity. Everything is as expected and no action is required."
                ),
                "samples": [(6, 6, None)],
                "result": (
                    "6 units were ordered in REX and 6 units were invoiced in NETO. "
                    "Perfect match — the order is processing exactly as planned with no discrepancies."
                ),
            },
        ],
    },
    {
        "title": "After invoice has been received into REX",
        "subtitle": "Goods physically arrived; quantities recorded against the PO.",
        "icon": "fa-box-open",
        "color": "emerald",
        "entries": [
            {
                "code": "BO Receival Error",
                "condition": "SKU is on the NETO backorder list (qty 0 invoiced) AND REX qty received > 0.",
                "description": (
                    "NETO told us this line was on backorder and shouldn't have shipped yet, "
                    "but the warehouse still physically received a non-zero quantity. "
                    "Almost always a picking error or a mix-up at dispatch. "
                    "Flag for warehouse review and reconcile physical stock."
                ),
                "samples": [(5, 0, 2)],
                "result": (
                    "5 units ordered, NETO shows 0 invoiced and the SKU is on the backorder list, "
                    "but 2 units were physically received. The stock shouldn't be here yet — "
                    "either the warehouse dispatched by mistake, or the receival was logged against the wrong line."
                ),
                "note": "Overrides 'Back-ordered' whenever received qty > 0.",
            },
            {
                "code": "IR Receival Error",
                "condition": "Parent NETO order is completed with this line skipped (Item Removed) AND REX qty received > 0.",
                "description": (
                    "NETO confirmed the warehouse skipped this SKU on the invoice — the line should not exist — "
                    "yet REX still recorded a non-zero receival. "
                    "Suggests a picking error or that stock was received against the wrong PO line. "
                    "Flag for warehouse review."
                ),
                "samples": [(4, None, 3)],
                "result": (
                    "4 units ordered. NETO completed the order with this line dropped off the invoice, "
                    "but 3 units still turned up in REX receival. Verify the physical stock and find out where the units came from."
                ),
                "note": "Overrides 'Item Removed' whenever the line was skipped on an otherwise-completed NETO order and received qty > 0.",
            },
            {
                "code": "Receival Difference",
                "condition": "NETO qty invoiced ≠ REX qty received (typical pick/ship mismatches).",
                "description": (
                    "The quantity physically received differs from what was invoiced. "
                    "Covers the usual pick/ship mistakes. "
                    "The specific backorder / item-removed sub-cases are now broken out separately as "
                    "BO Receival Error and IR Receival Error — everything else falls here. "
                    "Verify the physical count and record the discrepancy."
                ),
                "samples": [(5, 4, 5), (5, 5, 3), (5, 4, 3)],
                "result": (
                    "Examples: Row 1 — 4 invoiced but 5 received (received more). "
                    "Row 2 — 5 invoiced but 3 received (received less). "
                    "Row 3 — 4 invoiced but 3 received (both differ). "
                    "These scenarios indicate warehouse picking errors, shipping mistakes, "
                    "or incorrect physical counts during receiving."
                ),
            },
            {
                "code": "Quantity Changed",
                "condition": "REX Qty Ordered ≠ NETO qty invoiced = REX qty received.",
                "description": (
                    "All three quantities differ — the ordered, invoiced, and received amounts are all different. "
                    "This represents a complex discrepancy requiring careful reconciliation. "
                    "Document all differences with detailed notes."
                ),
                "samples": [(10, 8, 6)],
                "result": (
                    "10 units were ordered, 8 invoiced, and 6 received. "
                    "The warehouse reduced the quantity on the invoice, and then a smaller quantity was physically received. "
                    "Add a note explaining why the quantity changed at each step."
                ),
            },
            {
                "code": "Delivery Gap",
                "condition": "REX Qty Ordered = REX qty received but NETO qty invoiced ≠ REX qty received.",
                "description": (
                    "We received exactly what was ordered, but NETO's invoiced quantity disagrees. "
                    "Usually a paperwork mismatch rather than a physical discrepancy — "
                    "still worth a note so finance can reconcile."
                ),
                "samples": [(5, 4, 5)],
                "result": (
                    "5 units ordered, 5 received, but NETO only invoiced 4. "
                    "Physical fulfillment is correct; the gap is between the invoice and the receival record."
                ),
            },
            {
                "code": "No Change",
                "condition": "REX Qty Ordered = NETO qty invoiced = REX qty received.",
                "description": (
                    "Perfect match! All three quantities align correctly — ordered, invoiced, and received quantities are identical. "
                    "No discrepancies detected and no action required."
                ),
                "samples": [(6, 6, 6)],
                "result": (
                    "6 units ordered, 6 invoiced, and 6 received. "
                    "Perfect match across the entire order lifecycle. "
                    "This order was fulfilled exactly as planned with no discrepancies. No action required."
                ),
            },
        ],
    },
]


@purchase_orders_bp.route("/changelog-guide")
@login_required
def changelog_guide():
    return render_template(
        "purchase_orders/changelog_guide.html",
        stages=CHANGE_LOG_GUIDE_STAGES,
    )
