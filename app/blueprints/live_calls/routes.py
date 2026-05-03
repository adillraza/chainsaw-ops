"""Routes for the live-call webhook receiver and inspector."""
from __future__ import annotations

import json
import logging

from flask import Response, current_app, jsonify, request

from app.auth.abilities import require_capability
from app.blueprints.live_calls import live_calls_bp
from app.extensions import db
from app.models.call_events import CallEvent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public webhook endpoint
# ---------------------------------------------------------------------------

@live_calls_bp.route("/webhook", methods=["POST", "GET"])
def webhook():
    """Public RingCentral / CXone webhook target.

    Behaviour:

    * If the request carries a ``Validation-Token`` header (RC's subscription
      handshake), echo it back in the response header. RC then marks the
      subscription as verified.
    * Otherwise, capture the headers and body verbatim into ``call_event``,
      best-effort parse a few common fields, and return 200.
    * GET requests get a tiny ``ok`` response so visiting the URL in a
      browser confirms reachability without 405-ing.
    """
    # Reachability ping — useful for "is the URL alive?" checks.
    if request.method == "GET":
        return jsonify({"ok": True, "message": "live-calls webhook receiver"}), 200

    # RC subscription validation handshake
    validation_token = request.headers.get("Validation-Token")
    if validation_token:
        log.info("RC validation handshake received")
        resp = jsonify({"validated": True})
        resp.headers["Validation-Token"] = validation_token
        return resp, 200

    # Capture raw body + headers
    raw_body = request.get_data(as_text=True) or ""
    body_dict: dict | None = None
    try:
        body_dict = json.loads(raw_body) if raw_body else None
    except Exception:
        body_dict = None

    # Best-effort: identify source from headers / body shape
    source = _detect_source(request.headers, body_dict)

    # Try to pull a few common fields out of the payload so the inspector page
    # can show useful columns without us reading the JSON each time. Fall
    # through silently — the raw body is always stored.
    event_type = _pluck(body_dict, "eventType", "event", "type") if body_dict else None
    session_id = _pluck(
        body_dict, "telephonySessionId", "sessionId", "contactId", "callId"
    ) if body_dict else None
    from_number = _pluck(body_dict, "from.phoneNumber", "fromAddress", "ani", "caller") if body_dict else None
    to_number = _pluck(body_dict, "to.phoneNumber", "toAddress", "dnis", "called") if body_dict else None

    # Headers can contain anything, including secrets. Strip Authorization.
    headers_safe = {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "cookie")}

    evt = CallEvent(
        source=source,
        event_type=str(event_type)[:120] if event_type else None,
        session_id=str(session_id)[:120] if session_id else None,
        from_number=str(from_number)[:50] if from_number else None,
        to_number=str(to_number)[:50] if to_number else None,
        headers_json=json.dumps(headers_safe),
        body_json=raw_body or "{}",
    )
    db.session.add(evt)
    db.session.commit()

    log.info(
        "captured call_event id=%s source=%s type=%s from=%s",
        evt.id, evt.source, evt.event_type, evt.from_number,
    )

    # Return 200 ASAP. RC retries on non-2xx and we don't want to delay.
    return jsonify({"ok": True, "id": evt.id}), 200


# ---------------------------------------------------------------------------
# Inspector — capability-gated
# ---------------------------------------------------------------------------

@live_calls_bp.route("/events", methods=["GET"])
@require_capability("support.calls.view")
def events_list():
    """Recent webhook deliveries, newest first.

    Query params:
      * ``limit`` (default 50, max 200)
      * ``source`` (filter, e.g. ``ringcentral_pbx`` / ``cxone`` / ``test``)
      * ``from`` (filter on from_number contains)
    """
    limit = min(int(request.args.get("limit", 50)), 200)
    q = CallEvent.query.order_by(CallEvent.received_at.desc())
    if source := request.args.get("source"):
        q = q.filter(CallEvent.source == source)
    if needle := request.args.get("from"):
        q = q.filter(CallEvent.from_number.ilike(f"%{needle}%"))
    rows = q.limit(limit).all()
    return jsonify({
        "count": len(rows),
        "events": [{
            "id": r.id,
            "received_at": r.received_at.isoformat() + "Z",
            "source": r.source,
            "event_type": r.event_type,
            "session_id": r.session_id,
            "from_number": r.from_number,
            "to_number": r.to_number,
            "body_preview": (r.body_json or "")[:400],
        } for r in rows],
    })


@live_calls_bp.route("/events/<int:event_id>", methods=["GET"])
@require_capability("support.calls.view")
def event_detail(event_id: int):
    """Full single-event detail including raw body + headers."""
    evt = CallEvent.query.get_or_404(event_id)
    return jsonify({
        "id": evt.id,
        "received_at": evt.received_at.isoformat() + "Z",
        "source": evt.source,
        "event_type": evt.event_type,
        "session_id": evt.session_id,
        "from_number": evt.from_number,
        "to_number": evt.to_number,
        "headers": json.loads(evt.headers_json) if evt.headers_json else None,
        "body": json.loads(evt.body_json) if evt.body_json else None,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_source(headers, body) -> str:
    """Best-effort attribution of the inbound webhook to a known source.

    We look at a few signals:
      * User-Agent header (RC uses "RingCentral.Webhooks/...")
      * Custom test header (``X-Source: test``) for our own curl checks
      * Payload shape (CXone uses different JSON shape than RC PBX)
    """
    ua = (headers.get("User-Agent") or "").lower()
    custom = (headers.get("X-Source") or "").lower()
    if custom:
        return custom[:40]
    if "ringcentral" in ua:
        return "ringcentral_pbx"
    if isinstance(body, dict):
        if "contactId" in body or "agentId" in body:
            return "cxone"
        if "eventType" in body or "telephonySessionId" in body:
            return "ringcentral_pbx"
    return "unknown"


def _pluck(d: dict, *paths: str):
    """Look up the first non-empty value across dotted-path candidates.

    ``_pluck(d, "from.phoneNumber", "fromAddress")`` returns the first one that
    resolves to a truthy value, walking dotted segments through nested dicts.
    """
    if not isinstance(d, dict):
        return None
    for path in paths:
        cur = d
        for seg in path.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                cur = None
                break
        if cur:
            return cur
    return None
