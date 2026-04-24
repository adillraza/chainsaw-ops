from flask import Blueprint

admin_bp = Blueprint("admin", __name__)

from app.blueprints.admin import routes  # noqa: E402,F401
from app.blueprints.admin import role_routes  # noqa: E402,F401
