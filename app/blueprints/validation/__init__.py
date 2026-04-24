"""Validation blueprint: leadership/admin review of system-generated changes.

Currently hosts the MSL Changes page; future tabs (e.g. price changes,
supplier mismatches) should register here as additional routes.
"""
from flask import Blueprint

validation_bp = Blueprint(
    "validation",
    __name__,
    url_prefix="/validation",
)

from app.blueprints.validation import routes  # noqa: E402,F401
