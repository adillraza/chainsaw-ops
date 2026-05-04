"""Customer 360 routes."""
from __future__ import annotations

from flask import jsonify, render_template, request

from app.auth.abilities import require_capability
from app.blueprints.customer_360 import customer_360_bp
from app.services.customer_360_service import customer_360_service, normalize_phone


@customer_360_bp.route("/", methods=["GET"])
@require_capability("support.calls.view")
def index():
    """Search/landing page — manual phone-number entry for testing.

    Once the live-calls sidebar is wired up the agent rarely arrives here
    via URL; they click an active call. But the search-by-phone flow is
    the canonical fallback for manual lookups, and it's where ``/customer``
    sends you when no phone is supplied.
    """
    return render_template("customer_360/index.html")


@customer_360_bp.route("/<phone>", methods=["GET"])
@require_capability("support.calls.view")
def card(phone: str):
    """Render the customer card for the given phone number."""
    payload = customer_360_service.get_card(phone)
    return render_template("customer_360/card.html", c=payload)


@customer_360_bp.route("/api/<phone>", methods=["GET"])
@require_capability("support.calls.view")
def card_json(phone: str):
    """JSON variant of the card payload."""
    return jsonify(customer_360_service.get_card(phone))


# Convenience: a search-form POST that just redirects to /customer/<phone>.
# Lets the index search box use a plain HTML form with no JS.
@customer_360_bp.route("/search", methods=["POST"])
@require_capability("support.calls.view")
def search():
    raw = (request.form.get("phone") or "").strip()
    norm = normalize_phone(raw) or raw or "unknown"
    from flask import redirect, url_for
    return redirect(url_for("customer_360.card", phone=norm))
