"""PO Cross Check blueprint.

Each screen is a real URL that extends ``layouts/base.html`` (Tailwind /
Preline / HTMX) and uses the local SQLite cache
(:mod:`app.models.purchase_orders`) plus
:mod:`app.services.purchase_orders_service` for fresh data.
"""
from flask import Blueprint

purchase_orders_bp = Blueprint(
    "purchase_orders",
    __name__,
    url_prefix="/po",
    template_folder="../../templates/purchase_orders",
)

from app.blueprints.purchase_orders import routes  # noqa: E402,F401
