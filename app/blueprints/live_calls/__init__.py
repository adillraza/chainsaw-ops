"""Live-call webhook capture + inspector.

POST /api/calls/webhook       — public; RingCentral and CXone push events here.
GET  /api/calls/events        — capability-gated; recent events in the DB.
GET  /api/calls/events/<id>   — capability-gated; full single-event detail.

The webhook endpoint is intentionally public (no Flask-Login decorator) — RC's
servers don't authenticate via session cookies. Verification of inbound
payload authenticity (signed headers, validation tokens) lives inside the
route handler.
"""
from flask import Blueprint

live_calls_bp = Blueprint("live_calls", __name__, url_prefix="/api/calls")

from app.blueprints.live_calls import routes  # noqa: E402,F401
