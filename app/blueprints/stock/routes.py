"""REX Stock — search a SKU, see its full stock picture.

Live BigQuery lookup (one SKU at a time) so the movement history is always
current. Reads dataform.rex_ballarat_inventory + rex_inventory_movement_logs
via app.services.stock_service.
"""
from __future__ import annotations

from flask import render_template, request
from flask_login import login_required

from app.auth.abilities import require_capability
from app.blueprints.stock import stock_bp
from app.services.stock_service import get_stock_picture

# movement_type -> (direction label, tailwind text colour)
_TYPE_STYLE = {
    "Invoice":                       ("out", "text-red-600"),
    "PO":                            ("in",  "text-emerald-600"),
    "Manual Adjustment":             ("adj", "text-amber-600"),
    "Stock Take":                    ("count", "text-blue-600"),
    "Inventory Initialisation":      ("init", "text-slate-500"),
    "Inventory Fix":                 ("fix", "text-purple-600"),
}


@stock_bp.route("/")
@login_required
@require_capability("stock.view")
def index():
    sku = (request.args.get("sku") or "").strip()
    picture = get_stock_picture(sku) if sku else None
    return render_template(
        "stock/index.html",
        sku=sku,
        picture=picture,
        type_style=_TYPE_STYLE,
        searched=bool(sku),
    )
