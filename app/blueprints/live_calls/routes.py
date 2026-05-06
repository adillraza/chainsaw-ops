"""Routes for the live-call webhook receiver and inspector."""
from __future__ import annotations

import functools
import json
import logging

from flask import Response, current_app, jsonify, request
from flask_login import current_user, login_required

from app.auth.abilities import require_capability
from app.blueprints.live_calls import live_calls_bp
from app.extensions import db
from app.models.call_events import CallEvent, PinnedCall

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
    master_session_id = parsed.get("master_session_id")
    from_number = parsed.get("from_number")
    to_number = parsed.get("to_number")

    # Headers can contain anything, including secrets. Strip Authorization.
    headers_safe = {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "cookie")}

    evt = CallEvent(
        source=source,
        event_type=str(event_type)[:120] if event_type else None,
        session_id=str(session_id)[:120] if session_id else None,
        master_session_id=str(master_session_id)[:120] if master_session_id else None,
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

# How long a call lingers in the drawer after Disconnected before fading off.
# Lets the agent notice short calls they otherwise would have missed (e.g. a
# 2-second Setup→Disconnected sequence between two browser polls).
ENDED_LINGER_SECONDS = 15


@live_calls_bp.route("/active", methods=["GET"])
@require_capability("support.calls.view")
def active_calls():
    """Return one row per in-flight (or just-ended) call, latest event only.

    Source: ``call_event`` table on prod SQLite (every webhook delivery from
    RC PBX). One call generates many events (Setup → Proceeding → Alerting →
    Answered → Disconnected, often duplicated per-party). We collapse to one
    row per ``session_id`` showing the most recent event.

    Two display states:
      * **active** — latest event is NOT a Disconnected. Full opacity.
      * **recently_ended** — latest event IS a Disconnected, but received
        within the last ``ENDED_LINGER_SECONDS`` so the row stays clickable
        for a beat after the call drops. Rendered faded.

    Looks back 10 minutes for the active list — calls that never get a
    Disconnected webhook (rare, network drop) shouldn't haunt the drawer.

    Renders an HTML fragment by default (HTMX-friendly, swap ``innerHTML``
    into the drawer body). Pass ``?format=json`` for the JSON variant.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import func, and_

    since = datetime.utcnow() - timedelta(minutes=10)

    # Step 1 — pull the latest event per ``session_id`` (i.e. per CXone
    # contactId / RC telephony_session_id). Each leg of a transferred call
    # is its own session_id, so this still returns one row per leg.
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

    # Step 2 — collapse legs sharing a ``master_session_id`` into one card.
    # CXone warm transfers create a second contactId for the receiving
    # agent's leg; both share masterContactId. Without this step the same
    # caller would render twice in the drawer ("why is this person here
    # twice?"). PBX events have NULL master_session_id and fall through
    # to session_id via the COALESCE so they're unaffected.
    visible = _collapse_to_master(rows)

    if request.args.get("format") == "json":
        return jsonify({
            "count_active":   sum(1 for _, s, _, _ in visible if s == "active"),
            "count_lingering": sum(1 for _, s, _, _ in visible if s == "recently_ended"),
            "events": [_serialise_active(r) for r, _, _, _ in visible],
        })

    pinned_set = _pinned_session_ids_global()
    from flask import render_template
    return render_template(
        "partials/live_calls_drawer.html",
        active=[_active_view_model(r, state, secs_end, pinned_set, leg_count=lc)
                for r, state, secs_end, lc in visible],
        active_count=sum(1 for _, s, _, _ in visible if s == "active"),
        ended_count=sum(1 for _, s, _, _ in visible if s == "recently_ended"),
    )


def _collapse_to_master(rows: list) -> list[tuple]:
    """Group leg rows by ``COALESCE(master_session_id, session_id)``,
    then run a second pass that merges same-phone legs whose time
    windows overlap (cross-platform CXone↔PBX forwards).

    Returns ``[(primary_event, state, seconds_since_end, leg_count), …]``
    where:
      * ``primary_event`` is the most-active leg in the group:
        - if any leg is still in flight, the most recently updated non-
          terminal one is chosen — that's the agent currently on the call
        - if all legs have disconnected, the most recently ended leg wins
      * ``state`` is "active" when at least one leg is non-terminal,
        else "recently_ended" (within ``ENDED_LINGER_SECONDS``) — masters
        whose every leg ended longer ago than the linger window are
        dropped from the visible list.
      * ``leg_count`` is the number of legs collapsed into this card.
        The template renders a "transferred (N legs)" hint when > 1.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta

    # Pass 1: group by master_session_id (CXone transfers).
    legs_by_master: dict = defaultdict(list)
    for r in rows:
        key = r.master_session_id or r.session_id
        legs_by_master[key].append(r)

    now = datetime.utcnow()
    by_master = []   # [(primary, state, secs_end, leg_count, phone_norm)]
    for legs in legs_by_master.values():
        non_terminal = [r for r in legs if not _is_terminal(r.event_type)]
        if non_terminal:
            primary = max(non_terminal, key=lambda r: r.received_at)
            state, secs_end = "active", 0
        else:
            primary = max(legs, key=lambda r: r.received_at)
            secs_end = (now - primary.received_at).total_seconds()
            if secs_end > ENDED_LINGER_SECONDS:
                continue
            state = "recently_ended"
        # Track the earliest event in this group as the call's start —
        # used by the overlap-merge pass below.
        first_at = min(r.received_at for r in legs)
        last_at  = max(r.received_at for r in legs)
        # Normalise the phone so CXone (+61…) and PBX (0…) entries collide.
        raw = primary.from_number or ""
        phone_norm = "0" + raw[3:] if raw.startswith("+61") else raw
        by_master.append({
            "primary": primary, "state": state, "secs_end": secs_end,
            "leg_count": len(legs), "phone_norm": phone_norm,
            "first_at": first_at, "last_at": last_at,
        })

    # Pass 2: merge same-phone groups whose time windows overlap (within
    # a 10s buffer). This catches CXone-then-PBX forwards where both
    # platforms tracked the same physical call independently.
    by_master.sort(key=lambda g: (g["phone_norm"], g["first_at"]))
    BUFFER = timedelta(seconds=10)
    clusters: list[list[dict]] = []
    current: list[dict] = []
    running_max_end = None
    current_phone = None
    for g in by_master:
        if (current_phone is None
                or g["phone_norm"] != current_phone
                or g["first_at"] > (running_max_end + BUFFER)):
            if current:
                clusters.append(current)
            current = [g]
            current_phone = g["phone_norm"]
            running_max_end = g["last_at"]
        else:
            current.append(g)
            if g["last_at"] > running_max_end:
                running_max_end = g["last_at"]
    if current:
        clusters.append(current)

    out = []
    for cluster in clusters:
        if len(cluster) == 1:
            g = cluster[0]
            out.append((g["primary"], g["state"], g["secs_end"], g["leg_count"]))
            continue
        # Multi-leg cluster: pick the most-active leg as primary
        # (active over ended; among ended pick most recent).
        active = [g for g in cluster if g["state"] == "active"]
        chosen = max(active or cluster, key=lambda g: g["primary"].received_at)
        leg_count = sum(g["leg_count"] for g in cluster)
        out.append((chosen["primary"], chosen["state"], chosen["secs_end"], leg_count))

    # Newest first for the drawer
    out.sort(key=lambda x: x[0].received_at, reverse=True)
    return out


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


def _active_view_model(evt, state: str = "active", seconds_since_end: float = 0, pinned_set: set | None = None, leg_count: int = 1) -> dict:
    """Template-friendly view model — adds a normalised AU local phone +
    a short "ringing for X" duration string for the drawer card.

    ``state`` is either ``"active"`` (call still in flight) or
    ``"recently_ended"`` (latest event is a Disconnected within the linger
    window). The template uses this to render a faded card for ended calls.
    """
    from datetime import datetime

    def _au_local(s: str | None) -> str:
        s = s or ""
        return "0" + s[3:] if s.startswith("+61") else s

    norm_from = _au_local(evt.from_number)
    norm_to   = _au_local(evt.to_number)

    # If the "from" is a JJ-internal DID (staff DirectNumber, IVR DID),
    # the actual customer is on the "to" side — this is an outbound
    # staff→customer call where RC stamped the staff line as the caller.
    # Swap so the drawer card surfaces the real customer phone.
    swapped = _is_internal_phone(norm_from)
    if swapped:
        norm_phone = norm_to
        to_local   = norm_from   # the JJ line they dialled out from
    else:
        norm_phone = norm_from
        to_local   = norm_to

    # Pull the bare status code from "Direction:Status" composite
    status_code = (evt.event_type or "").split(":", 1)[-1] if evt.event_type else ""
    direction = (evt.event_type or "").split(":", 1)[0] if ":" in (evt.event_type or "") else None
    # When swapped, present this as outbound regardless of how the source
    # system labelled the leg — the agent's mental model is "we called them".
    if swapped:
        direction = "Outbound"
    secs = int((datetime.utcnow() - evt.received_at).total_seconds()) if evt.received_at else 0
    return {
        "session_id":   evt.session_id,
        "raw_phone":    norm_phone,
        "phone":        norm_phone,
        "customer_name": _resolve_customer_name(norm_phone),
        "to_number":    evt.to_number,
        "to_local":     to_local,
        "status_code":  status_code,
        "direction":    direction,
        "source":       evt.source,
        "received_at":  evt.received_at,
        "seconds_old":  secs,
        # State flags drive template styling
        "state":        state,
        "is_active":    state == "active",
        "is_recently_ended": state == "recently_ended",
        "seconds_since_end": int(seconds_since_end),
        "is_ringing":   state == "active" and status_code in ("Setup", "Proceeding", "Alerting"),
        "is_connected": state == "active" and status_code in ("Answered", "Hold"),
        "is_pinned":    bool(pinned_set and (evt.session_id in pinned_set or (evt.master_session_id and evt.master_session_id in pinned_set))),
        # > 1 means this master had multiple legs (e.g. agent-to-agent
        # warm transfer); template can show a small "transferred" hint.
        "leg_count":    leg_count,
        "is_transferred": leg_count > 1,
    }


def _resolve_customer_name(phone: str) -> str | None:
    """Look up the display name for a phone via the LRU-cached
    ``Customer360Service.get_name_for_phone`` on the module-level
    singleton. Soft-fails to ``None`` so a BigQuery hiccup never
    breaks the drawer render.

    Returns ``None`` for JJ-internal phones — these previously
    matched a fake customer record (e.g. 0353030263 → "Bill Parker"
    via a phone-lookup typo) and surfaced the wrong name on every
    contact-centre call.
    """
    if not phone:
        return None
    if _is_internal_phone(phone):
        return None
    try:
        from app.services.customer_360_service import customer_360_service
        return customer_360_service.get_name_for_phone(phone)
    except Exception:
        return None


def _is_internal_phone(phone: str) -> bool:
    """Cheap PK lookup against ``internal_phone_numbers`` SQLite table.

    Cached aggressively at module level — the table is 25 rows and
    changes maybe once a quarter (when a new staff member joins or a
    new RC line is provisioned). Restart the service to bust the cache
    after a refresh; that matches the existing operational pattern.
    """
    if not phone:
        return False
    return phone in _internal_phones_set()


@functools.lru_cache(maxsize=1)
def _internal_phones_set() -> frozenset:
    try:
        from app.models.internal_phone import InternalPhoneNumber
        rows = InternalPhoneNumber.query.with_entities(InternalPhoneNumber.phone).all()
        return frozenset(r[0] for r in rows)
    except Exception:
        return frozenset()


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
            # PBX legs already collapse via telephony_session_id, so master
            # is left NULL — group-by COALESCE then falls through to session_id.
            "master_session_id": None,
            "from_number": (p.get("from") or {}).get("phoneNumber"),
            "to_number":   (p.get("to") or {}).get("phoneNumber"),
        }

    # --- CXone (Studio Snippet posts a flat JSON we control) ---
    if "contactId" in body or "fromAddress" in body:
        contact_id = body.get("contactId") or body.get("masterContactId")
        master_id = body.get("masterContactId") or contact_id
        return {
            "event_type":  body.get("eventType") or "cxone.event",
            "session_id":  contact_id,
            # Always populated for CXone — equals contactId for non-transfer
            # calls, equals the originating contactId for transferred legs.
            "master_session_id": master_id,
            "from_number": body.get("fromAddress") or body.get("ANI"),
            "to_number":   body.get("toAddress")   or body.get("DNIS"),
        }

    # --- Fallback: best-effort generic pluck ---
    return {
        "event_type":  _pluck(body, "eventType", "event", "type"),
        "session_id":  _pluck(body, "telephonySessionId", "sessionId", "contactId", "callId"),
        "master_session_id": _pluck(body, "masterContactId"),
        "from_number": _pluck(body, "from.phoneNumber", "fromAddress", "ani", "caller"),
        "to_number":   _pluck(body, "to.phoneNumber",   "toAddress",   "dnis", "called"),
    }


def reparse_all_call_events():
    """Re-extract event_type / session_id / master / from / to from
    stored ``body_json`` and update each row in place.

    Handles both shapes the receiver might have stored:
      * JSON (RC PBX webhook deliveries)
      * form-urlencoded (CXone poller posts ``application/x-www-form-urlencoded``)

    Wired up as the ``reparse-call-events`` Flask CLI command (see
    :mod:`app.cli`). Used after deploying a parser change so the
    inspector shows the new fields on rows captured before the fix.
    Returns ``(updated_count, total_count)``.
    """
    from urllib.parse import parse_qs as _parse_qs

    rows = CallEvent.query.all()
    n = 0
    for r in rows:
        raw = r.body_json or ""
        body = None
        if raw.startswith("{"):
            try:
                body = json.loads(raw)
            except Exception:
                body = None
        elif "=" in raw:
            try:
                parsed = _parse_qs(raw, keep_blank_values=True)
                body = {k: (v[0] if v else "") for k, v in parsed.items()}
            except Exception:
                body = None
        if body is None:
            continue

        parsed = _parse_event(body, r.source)
        et  = parsed.get("event_type")
        sid = parsed.get("session_id")
        msid = parsed.get("master_session_id")
        fn  = parsed.get("from_number")
        tn  = parsed.get("to_number")
        if (
            et != r.event_type or sid != r.session_id
            or msid != r.master_session_id
            or fn != r.from_number or tn != r.to_number
        ):
            r.event_type = (str(et)[:120] if et else None)
            r.session_id = (str(sid)[:120] if sid else None)
            r.master_session_id = (str(msid)[:120] if msid else None)
            r.from_number = (str(fn)[:50] if fn else None)
            r.to_number = (str(tn)[:50] if tn else None)
            n += 1
    db.session.commit()
    return n, len(rows)


# ---------------------------------------------------------------------------
# Recent calls section — last 20 ended sessions
# ---------------------------------------------------------------------------

@live_calls_bp.route("/recent", methods=["GET"])
@require_capability("support.calls.view")
def recent_calls():
    """Return the last 20 *ended* call sessions (one row per session_id).

    Distinct from /active (which only shows in-flight). Recent shows calls
    whose latest event is a Disconnected — the agent's "I just hung up,
    let me look up the customer" buffer.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import func, and_
    from flask import render_template

    since = datetime.utcnow() - timedelta(hours=12)

    # Latest event per session
    latest = (
        db.session.query(
            CallEvent.session_id.label("sid"),
            func.max(CallEvent.received_at).label("latest_at"),
            func.min(CallEvent.received_at).label("first_at"),
        )
        .filter(CallEvent.received_at > since)
        .filter(CallEvent.session_id.isnot(None))
        .group_by(CallEvent.session_id)
        .subquery()
    )

    rows = (
        db.session.query(CallEvent, latest.c.first_at)
        .join(
            latest,
            and_(
                CallEvent.session_id == latest.c.sid,
                CallEvent.received_at == latest.c.latest_at,
            ),
        )
        .order_by(latest.c.latest_at.desc())
        .all()
    )

    # Pass 1 — collapse legs that share a masterContactId (CXone transfers).
    from collections import defaultdict
    from datetime import timedelta
    legs_by_master: dict = defaultdict(list)
    for evt, first_at in rows:
        key = evt.master_session_id or evt.session_id
        legs_by_master[key].append((evt, first_at))

    pinned_session_ids = _pinned_session_ids_global()
    by_master = []
    for legs in legs_by_master.values():
        # /recent only shows fully-ended calls; if any leg is still alive
        # the master belongs to /active.
        if not all(_is_terminal(evt.event_type) for evt, _ in legs):
            continue
        primary_evt, primary_first = max(legs, key=lambda x: x[0].received_at)
        earliest_first = min((fa for _, fa in legs if fa), default=primary_first)
        # Phone normalised so CXone (+61…) and PBX (0…) cluster together.
        raw = primary_evt.from_number or ""
        phone_norm = "0" + raw[3:] if raw.startswith("+61") else raw
        by_master.append({
            "primary": primary_evt,
            "first_at": earliest_first,
            "last_at": primary_evt.received_at,
            "leg_count": len(legs),
            "phone_norm": phone_norm,
        })

    # Pass 2 — merge same-phone groups whose time windows overlap (within
    # 10s). Catches CXone-rings-then-forwards-to-PBX scenarios where the
    # two platforms tracked the same physical call as separate sessions.
    by_master.sort(key=lambda g: (g["phone_norm"], g["first_at"] or g["last_at"]))
    BUFFER = timedelta(seconds=10)
    clusters: list[list[dict]] = []
    current: list[dict] = []
    running_max_end = None
    current_phone = None
    for g in by_master:
        start = g["first_at"] or g["last_at"]
        if (current_phone is None
                or g["phone_norm"] != current_phone
                or start > (running_max_end + BUFFER)):
            if current:
                clusters.append(current)
            current = [g]
            current_phone = g["phone_norm"]
            running_max_end = g["last_at"]
        else:
            current.append(g)
            if g["last_at"] > running_max_end:
                running_max_end = g["last_at"]
    if current:
        clusters.append(current)

    collapsed = []
    for cluster in clusters:
        if len(cluster) == 1:
            g = cluster[0]
            collapsed.append((g["primary"], g["first_at"], g["leg_count"]))
            continue
        # Multi-platform cluster: pick the longest leg as primary (the
        # platform where the conversation actually happened), keep the
        # earliest first_at so the duration spans the whole call.
        chosen = max(cluster, key=lambda g: (g["last_at"] - (g["first_at"] or g["last_at"])).total_seconds())
        earliest = min((g["first_at"] for g in cluster if g["first_at"]), default=chosen["first_at"])
        leg_count = sum(g["leg_count"] for g in cluster)
        collapsed.append((chosen["primary"], earliest, leg_count))

    # Newest first, cap at 20
    collapsed.sort(key=lambda x: x[0].received_at, reverse=True)
    ended = [
        _recent_view_model(evt, first_at, pinned_session_ids, leg_count=lc)
        for evt, first_at, lc in collapsed[:20]
    ]

    return render_template("partials/live_calls_recent.html", recent=ended)


def _recent_view_model(evt, first_at, pinned_session_ids: set, leg_count: int = 1) -> dict:
    """Build a card view model for a recently-ended session."""
    raw_phone = evt.from_number or ""
    norm_from = "0" + raw_phone[3:] if raw_phone.startswith("+61") else raw_phone
    raw_to = evt.to_number or ""
    norm_to = "0" + raw_to[3:] if raw_to.startswith("+61") else raw_to

    # Same swap as _active_view_model: when "from" is a JJ-internal DID,
    # the actual customer is in "to". Stops a contact-centre outbound
    # call from displaying as "Bill Parker · 0353030263".
    if _is_internal_phone(norm_from):
        phone    = norm_to
        to_local = norm_from
        direction = "Outbound"
    else:
        phone    = norm_from
        to_local = norm_to
        direction = (evt.event_type or "").split(":", 1)[0] if ":" in (evt.event_type or "") else None

    duration_s = int((evt.received_at - first_at).total_seconds()) if first_at else 0

    # Pull agentName from body if it's there
    agent_name = None
    try:
        from urllib.parse import parse_qs as _parse_qs
        raw = evt.body_json or ""
        if raw.startswith("{"):
            body = json.loads(raw)
        else:
            parsed = _parse_qs(raw, keep_blank_values=True)
            body = {k: (v[0] if v else "") for k, v in parsed.items()}
        agent_name = (body.get("agentName") or "").strip() or None
    except Exception:
        body = {}

    from app.template_filters import utc_to_mel_naive
    return {
        "session_id":    evt.session_id,
        "phone":         phone,
        "customer_name": _resolve_customer_name(phone),
        "to_local":      to_local,
        "direction":     direction,
        "source":        evt.source,
        # call_event.received_at is naive UTC; templates assume naive=Mel.
        "ended_at":      utc_to_mel_naive(evt.received_at),
        "duration_s":    duration_s,
        "agent_name":    agent_name,
        "is_pinned":     evt.session_id in pinned_session_ids or (evt.master_session_id and evt.master_session_id in pinned_session_ids),
        "leg_count":     leg_count,
        "is_transferred": leg_count > 1,
    }


def _pinned_session_ids_global() -> set:
    """Return the set of session_ids pinned by anyone (team-shared pins)."""
    rows = PinnedCall.query.with_entities(PinnedCall.session_id).all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Pin Calls section
# ---------------------------------------------------------------------------

@live_calls_bp.route("/pinned", methods=["GET"])
@require_capability("support.calls.view")
def pinned_calls():
    """List ALL pinned calls (team-shared), newest pin first."""
    from flask import render_template
    rows = (
        PinnedCall.query
        .order_by(PinnedCall.pinned_at.desc())
        .all()
    )
    return render_template("partials/live_calls_pinned.html", pinned=rows)


@live_calls_bp.route("/pin/<path:session_id>", methods=["POST"])
@require_capability("support.calls.view")
def pin_call(session_id: str):
    """Pin a call. Team-shared and idempotent — re-pinning a session
    silently no-ops. Snapshots display fields from the latest call_event
    plus the resolved customer name so the pin keeps rendering even
    after the source rows are pruned.
    """
    existing = PinnedCall.query.filter_by(session_id=session_id).first()
    if existing:
        return ("", 204)

    # Snapshot from the latest call_event row for this session
    evt = (
        CallEvent.query
        .filter(CallEvent.session_id == session_id)
        .order_by(CallEvent.received_at.desc())
        .first()
    )
    agent_name = None
    skill = None
    if evt:
        try:
            from urllib.parse import parse_qs as _parse_qs
            raw = evt.body_json or ""
            if raw.startswith("{"):
                body = json.loads(raw)
            else:
                parsed = _parse_qs(raw, keep_blank_values=True)
                body = {k: (v[0] if v else "") for k, v in parsed.items()}
            agent_name = (body.get("agentName") or "").strip() or None
            skill = body.get("skill") or None
        except Exception:
            pass

    direction = None
    status_at_pin = None
    if evt and evt.event_type:
        if ":" in evt.event_type:
            direction, status_at_pin = evt.event_type.split(":", 1)
        else:
            status_at_pin = evt.event_type

    # Resolve the caller name now so the pin survives after BQ refresh
    raw_phone = evt.from_number if evt else None
    phone_local = ("0" + raw_phone[3:]) if (raw_phone and raw_phone.startswith("+61")) else (raw_phone or "")
    customer_name = _resolve_customer_name(phone_local) if phone_local else None

    pin = PinnedCall(
        session_id=session_id,
        pinned_by_user_id=current_user.id if getattr(current_user, "is_authenticated", False) else None,
        phone=raw_phone,
        to_number=evt.to_number if evt else None,
        direction=direction,
        status_at_pin=status_at_pin,
        source=evt.source if evt else None,
        agent_name=agent_name,
        skill=skill,
        customer_name=customer_name,
    )
    db.session.add(pin)
    db.session.commit()
    return ("", 204)


@live_calls_bp.route("/pin/<path:session_id>", methods=["DELETE"])
@require_capability("support.calls.view")
def unpin_call(session_id: str):
    """Unpin a call. Team-shared — anyone can unpin any pin."""
    PinnedCall.query.filter_by(session_id=session_id).delete()
    db.session.commit()
    return ("", 204)
