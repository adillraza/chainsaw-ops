"""Customer 360 routes."""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from flask import current_app, jsonify, render_template, request

from app.auth.abilities import require_capability
from app.blueprints.customer_360 import customer_360_bp
from app.services.customer_360_service import customer_360_service, normalize_phone


# Short-lived signing for the /listen WSS endpoint on rcx-stream-server.
# The receiver validates with the same secret + algorithm.
LISTEN_TOKEN_TTL_SECONDS = 5 * 60


@customer_360_bp.route("/", methods=["GET"])
@require_capability("support.calls.view")
def index():
    """Search/landing page — manual phone-number entry for testing.

    Once the live-calls sidebar is wired up the agent rarely arrives here
    via URL; they click an active call. But the search-by-phone flow is
    the canonical fallback for manual lookups, and it's where ``/customer``
    sends you when no phone is supplied.
    """
    return render_template("customer_360/index.html",
                           page_title="Customer 360",
                           cache_context="customer_360")


@customer_360_bp.route("/<phone>", methods=["GET"])
@require_capability("support.calls.view")
def card(phone: str):
    """Render the customer card for the given phone number."""
    payload = customer_360_service.get_card(phone)
    # Attach any in-flight call so the template can render the "Call in
    # progress" panel at the top.
    payload["active_call"] = customer_360_service.get_active_call_for_phone(phone)
    return render_template("customer_360/card.html", c=payload,
                           page_title="Customer 360",
                           cache_context="customer_360")


@customer_360_bp.route("/api/<phone>", methods=["GET"])
@require_capability("support.calls.view")
def card_json(phone: str):
    """JSON variant of the card payload."""
    return jsonify(customer_360_service.get_card(phone))


@customer_360_bp.route("/api/listen-token/<phone>", methods=["GET"])
@require_capability("support.calls.view")
def listen_token(phone: str):
    """Issue a short-lived HMAC token for the live-audio /listen WSS.

    The receiver process (scripts/rcx_stream_server.py) shares the same
    signing secret and validates the token before attaching the browser
    to a call's audio fan-out. Token lives ~5 minutes; the player
    refreshes before expiry if the call is still in flight.
    """
    norm = normalize_phone(phone) or ""
    secret_str = (os.environ.get("RCX_LISTEN_SECRET")
                  or current_app.config.get("SECRET_KEY") or "")
    if not norm or not secret_str:
        return jsonify({"error": "unavailable"}), 503
    expiry = int(time.time()) + LISTEN_TOKEN_TTL_SECONDS
    sig = hmac.new(secret_str.encode(), f"{norm}:{expiry}".encode(),
                   hashlib.sha256).hexdigest()[:16]
    return jsonify({
        "phone":     norm,
        "expires_at": expiry,
        "token":     f"{expiry}:{sig}",
    })


@customer_360_bp.route("/api/call/<path:session_id>", methods=["GET"])
@require_capability("support.calls.view")
def call_details(session_id: str):
    """HTML partial — rendered inside the call-details modal via HTMX swap.

    ``path`` converter (not ``string``) so PBX session ids that contain dots
    (``s-a035ddc2def3bz...``) come through unmangled.
    """
    details = customer_360_service.get_call_details(session_id)
    return render_template("customer_360/_call_details_modal.html", d=details)


# Convenience: a search-form POST that just redirects to /customer/<phone>.
# Lets the index search box use a plain HTML form with no JS.
@customer_360_bp.route("/search", methods=["POST"])
@require_capability("support.calls.view")
def search():
    raw = (request.form.get("phone") or "").strip()
    norm = normalize_phone(raw) or raw or "unknown"
    from flask import redirect, url_for
    return redirect(url_for("customer_360.card", phone=norm))
