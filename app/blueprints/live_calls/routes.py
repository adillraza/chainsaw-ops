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

    # Capture raw body + headers. Accept JSON (RC PBX) or form-encoded
    # (CXone Studio's Rest Api Action only sends form-encoded). Whichever
    # format arrives, we end up with a flat dict in body_dict.
    raw_body = request.get_data(as_text=True) or ""
    body_dict: dict | None = None
    try:
        body_dict = json.loads(raw_body) if raw_body else None
    except Exception:
        body_dict = None
    # If body wasn't JSON, fall back to form-encoded parsing (request.form
    # is parsed by Flask from the same raw bytes when Content-Type matches).
    if body_dict is None and request.form:
        body_dict = {k: v for k, v in request.form.items()}

    # Best-effort: identify source from headers / body shape
    source = _detect_source(request.headers, body_dict)

    # Pull a few common fields out of the payload so the inspector page
    # can show useful columns without us reading the JSON each time.
    parsed = _parse_event(body_dict, source) if body_dict else {}
    event_type = parsed.get("event_type")
    session_id = parsed.get("session_id")
    from_number = parsed.get("from_number")
    to_number = parsed.get("to_number")

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
# Active-calls feed — backs the live drawer
# ---------------------------------------------------------------------------

# Statuses that mean the call is still in flight (caller hasn't hung up,
# agent hasn't dropped). Anything containing "Disconnected" is filtered out.
_STILL_ACTIVE_KEYWORDS = ("Setup", "Proceeding", "Alerting", "Answered", "Hold")


@live_calls_bp.route("/active", methods=["GET"])
@require_capability("support.calls.view")
def active_calls():
    """Return one row per in-flight call, latest event only.

    Source: ``call_event`` table on prod SQLite (every webhook delivery from
    RC PBX). One call generates many events (Setup → Proceeding → Alerting →
    Answered → Disconnected, often duplicated per-party). We collapse to one
    row per ``session_id`` showing the most recent event, then drop any whose
    most-recent event is a Disconnected.

    Looks back 10 minutes only — calls that never get a Disconnected event
    (rare, but possible if RC drops a webhook delivery) shouldn't haunt the
    drawer forever.

    Renders an HTML fragment by default (HTMX-friendly, swap ``innerHTML``
    into the drawer body). Pass ``?format=json`` for the JSON variant.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, and_

    since = datetime.utcnow() - timedelta(minutes=10)

    # Subquery: max(received_at) per session_id within the window
    latest_per_session = (
        db.session.query(
            CallEvent.session_id.label("sid"),
            func.max(CallEvent.received_at).label("latest_at"),
        )
        .filter(CallEvent.received_at > since)
        .filter(CallEvent.session_id.isnot(None))
        .group_by(CallEvent.session_id)
        .subquery()
    )

    # Join back to get the row for each (session_id, latest_at)
    rows = (
        db.session.query(CallEvent)
        .join(
            latest_per_session,
            and_(
                CallEvent.session_id == latest_per_session.c.sid,
                CallEvent.received_at == latest_per_session.c.latest_at,
            ),
        )
        .order_by(CallEvent.received_at.desc())
        .all()
    )

    # Filter to "still active" — latest event is NOT a Disconnected
    active = [r for r in rows if not _is_terminal(r.event_type)]

    if request.args.get("format") == "json":
        return jsonify({
            "count": len(active),
            "active": [_serialise_active(r) for r in active],
        })

    from flask import render_template
    return render_template(
        "partials/live_calls_drawer.html",
        active=[_active_view_model(r) for r in active],
    )


def _is_terminal(event_type: str | None) -> bool:
    """Return True when the latest event indicates the call is over."""
    if not event_type:
        return False
    et = event_type.lower()
    return "disconnected" in et


def _serialise_active(evt) -> dict:
    return {
        "session_id":  evt.session_id,
        "phone":       evt.from_number,
        "to_number":   evt.to_number,
        "status":      evt.event_type,
        "received_at": evt.received_at.isoformat() + "Z",
        "source":      evt.source,
    }


def _active_view_model(evt) -> dict:
    """Template-friendly view model — adds a normalised AU local phone +
    a short "ringing for X" duration string for the drawer card.
    """
    from datetime import datetime
    raw_phone = evt.from_number or ""
    # Normalise +61… → 0… so the drawer matches the URL the customer card uses
    if raw_phone.startswith("+61"):
        norm_phone = "0" + raw_phone[3:]
    else:
        norm_phone = raw_phone
    # Pull the bare status code from "Direction:Status" composite
    status_code = (evt.event_type or "").split(":", 1)[-1] if evt.event_type else ""
    direction = (evt.event_type or "").split(":", 1)[0] if ":" in (evt.event_type or "") else None
    secs = int((datetime.utcnow() - evt.received_at).total_seconds()) if evt.received_at else 0
    return {
        "session_id":   evt.session_id,
        "raw_phone":    raw_phone,
        "phone":        norm_phone,
        "to_number":    evt.to_number,
        "status_code":  status_code,
        "direction":    direction,
        "source":       evt.source,
        "received_at":  evt.received_at,
        "seconds_old":  secs,
        # The "still ringing" states get visual emphasis in the template
        "is_ringing":   status_code in ("Setup", "Proceeding", "Alerting"),
        "is_connected": status_code in ("Answered", "Hold"),
    }


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


def _parse_event(body: dict, source: str) -> dict:
    """Extract event_type / session_id / from_number / to_number from the
    real payload shapes we see in the wild.

    RingCentral PBX (telephony.sessions) wraps the actual call data in
    ``body.parties[0]``. CXone (when wired) sends a flat shape with
    ``contactId`` / ``fromAddress`` at the top level.

    Falls back to the generic dotted-path lookup for unknown shapes so we
    don't lose information from sources we haven't characterised yet.
    """
    if not isinstance(body, dict):
        return {}

    # --- RingCentral PBX telephony.sessions ---
    inner = body.get("body") if isinstance(body.get("body"), dict) else None
    parties = inner.get("parties") if inner else None
    if isinstance(parties, list) and parties:
        # The first party is the inbound side from RC's perspective. There
        # can be multiple parties for transferred / forwarded calls; for
        # display we keep it simple and use party 0.
        p = parties[0] if isinstance(parties[0], dict) else {}
        status_code = ((p.get("status") or {}).get("code")) or "?"
        direction = p.get("direction") or "?"
        return {
            "event_type":  f"{direction}:{status_code}",
            "session_id":  inner.get("telephonySessionId") or inner.get("sessionId"),
            "from_number": (p.get("from") or {}).get("phoneNumber"),
            "to_number":   (p.get("to") or {}).get("phoneNumber"),
        }

    # --- CXone (Studio Snippet posts a flat JSON we control) ---
    if "contactId" in body or "fromAddress" in body:
        return {
            "event_type":  body.get("eventType") or "cxone.event",
            "session_id":  body.get("contactId") or body.get("masterContactId"),
            "from_number": body.get("fromAddress") or body.get("ANI"),
            "to_number":   body.get("toAddress")   or body.get("DNIS"),
        }

    # --- Fallback: best-effort generic pluck ---
    return {
        "event_type":  _pluck(body, "eventType", "event", "type"),
        "session_id":  _pluck(body, "telephonySessionId", "sessionId", "contactId", "callId"),
        "from_number": _pluck(body, "from.phoneNumber", "fromAddress", "ani", "caller"),
        "to_number":   _pluck(body, "to.phoneNumber",   "toAddress",   "dnis", "called"),
    }


def reparse_all_call_events():
    """Re-extract event_type / session_id / from / to from stored body_json.

    Wired up as the ``reparse-call-events`` Flask CLI command (see
    :mod:`app.cli`). Used after deploying a parser change so the inspector
    shows the new fields on rows captured before the fix.
    """
    rows = CallEvent.query.all()
    n = 0
    for r in rows:
        try:
            body = json.loads(r.body_json) if r.body_json else None
        except Exception:
            continue
        parsed = _parse_event(body, r.source)
        et = parsed.get("event_type")
        sid = parsed.get("session_id")
        fn = parsed.get("from_number")
        tn = parsed.get("to_number")
        if (
            et != r.event_type or sid != r.session_id
            or fn != r.from_number or tn != r.to_number
        ):
            r.event_type = (str(et)[:120] if et else None)
            r.session_id = (str(sid)[:120] if sid else None)
            r.from_number = (str(fn)[:50] if fn else None)
            r.to_number = (str(tn)[:50] if tn else None)
            n += 1
    db.session.commit()
    return n, len(rows)
