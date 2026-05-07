from flask import Blueprint

auth_bp = Blueprint("auth", __name__)

from app.blueprints.auth import routes      # noqa: E402,F401
from app.blueprints.auth import microsoft   # noqa: E402,F401
