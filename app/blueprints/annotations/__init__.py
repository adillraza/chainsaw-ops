from flask import Blueprint

annotations_bp = Blueprint("annotations", __name__, url_prefix="/annotations")

from app.blueprints.annotations import routes  # noqa: E402,F401
