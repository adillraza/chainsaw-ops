"""Legacy ``/api/*`` JSON endpoints.

These routes back the cache-status indicator, refresh actions, review
mutations, and notes that the v2 templates still call into via fetch/HTMX.
Phase 5c will split them between a long-lived ``system_api`` blueprint and a
slim compatibility shim.
"""
from flask import Blueprint

legacy_api_bp = Blueprint("legacy_api", __name__, url_prefix="/api")

from app.blueprints.legacy_api import bigquery_routes  # noqa: E402,F401
from app.blueprints.legacy_api import notes_routes  # noqa: E402,F401
from app.blueprints.legacy_api import reviews_routes  # noqa: E402,F401
