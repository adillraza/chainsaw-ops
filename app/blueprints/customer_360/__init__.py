"""Customer 360 — live-call customer card.

Routes:

  GET /customer/<phone>        — render the full HTML card
  GET /customer/                — search box (manual phone entry for testing)
  GET /api/customer/<phone>    — JSON payload (used by the dashboard sidebar
                                  and any future SPA / iframe consumer)

All routes are gated on the ``support.calls.view`` capability.
"""
from flask import Blueprint

customer_360_bp = Blueprint(
    "customer_360",
    __name__,
    url_prefix="/customer",
    template_folder="../../templates",
)

from app.blueprints.customer_360 import routes  # noqa: E402,F401
