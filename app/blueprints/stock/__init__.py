"""REX Stock blueprint — per-SKU stock picture (movement history + balance)."""
from flask import Blueprint

stock_bp = Blueprint("stock", __name__, url_prefix="/stock")

from app.blueprints.stock import routes  # noqa: E402,F401
