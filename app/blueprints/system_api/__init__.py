"""System / cache-management JSON endpoints (cache status + refresh).

These power the topbar status pill, the dashboard ``Refresh now`` action and
the ``/dashboard-progress`` polling loop. They live in their own blueprint
(``/api/system/*``) so the catch-all ``legacy_api`` blueprint can be kept as
a thin compatibility shim while v2 templates target the new URLs directly.
"""
from flask import Blueprint

system_api_bp = Blueprint("system_api", __name__, url_prefix="/api/system")

from app.blueprints.system_api import routes  # noqa: E402,F401
