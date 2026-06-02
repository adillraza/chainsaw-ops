"""Services blueprint: staff-facing operational tools.

A container for self-contained "service" tools that sit outside the core
PO / reviews / customer-service workflows. The first is the **NETO Shippings**
tab — a visualisation of the live Neto shipping configuration (carriers,
categories, options, rate tables and how they're wired together), backed by a
structured snapshot in BigQuery.

Future services (e.g. the Startrack Freight Calculator) register here as
additional routes, each gated on its own ``services.*`` capability.
"""
from flask import Blueprint

services_bp = Blueprint(
    "services",
    __name__,
    url_prefix="/services",
)

from app.blueprints.services import routes  # noqa: E402,F401
