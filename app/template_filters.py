"""Custom Jinja filters registered globally on the Flask app.

Centralised so templates can rely on a small set of consistent helpers instead
of inlining ad-hoc strftime calls and `or '—'` fallbacks everywhere.
"""
from __future__ import annotations

from datetime import date, datetime, time

import pytz

EM_DASH = "—"
MEL = pytz.timezone("Australia/Melbourne")


def utc_to_mel_naive(dt: datetime | None) -> datetime | None:
    """Convert a naive-UTC datetime to a naive-Mel datetime.

    Use at the SQLite boundary: ``call_event.received_at``,
    ``pinned_call.pinned_at`` and friends default to ``datetime.utcnow()``,
    which produces a naive UTC value. The format_dt convention is
    "naive = already Mel" (because most of our Dataform models pin to
    Mel), so SQLite values must be shifted before they reach a template.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(MEL).replace(tzinfo=None)
    return pytz.utc.localize(dt).astimezone(MEL).replace(tzinfo=None)


def _strip_leading_zero(token: str) -> str:
    """Strip a single leading zero in an hour token (e.g. ``05`` -> ``5``)."""
    return token[1:] if token.startswith("0") and len(token) > 1 else token


def format_dt(value, fmt: str = "datetime") -> str:
    """Format a date/datetime in a human-friendly way.

    fmt:
      - ``datetime`` (default): ``13 Apr 2026, 2:35 PM`` if a time component is
        present, otherwise just ``13 Apr 2026``.
      - ``date``: always ``13 Apr 2026`` (drops the time).
      - ``time``: just ``2:35 PM``.
      - any other string: passed straight to ``strftime``.

    Returns an em-dash for empty/null values so templates don't have to
    repeat the ``or '—'`` dance.
    """
    if not value:
        return EM_DASH

    # SQLAlchemy DateTime columns hand back ``datetime``; pure ``date`` columns
    # return ``date``. Treat date-only inputs as midnight so we can branch on
    # whether the value carries a useful time component.
    if isinstance(value, datetime):
        dt = value
        # Convention:
        #   * tz-aware datetimes (e.g. BigQuery TIMESTAMP, comes back UTC) are
        #     converted to Mel here.
        #   * naive datetimes are assumed to ALREADY be in Mel — most of
        #     our Dataform models pin DATETIME columns to Australia/Melbourne
        #     so the card displays a local timestamp without any client-side
        #     conversion.
        # Anything coming from SQLite (``datetime.utcnow()`` defaults) is
        # naive UTC and must be converted to Mel BEFORE reaching this filter,
        # at the service / view-model boundary. Otherwise it's rendered as if
        # already-Mel and ends up displayed 10 hours behind reality.
        if dt.tzinfo is not None:
            dt = dt.astimezone(MEL)
    elif isinstance(value, date):
        dt = datetime.combine(value, time())
    else:
        return str(value)

    if fmt == "date":
        return dt.strftime("%d %b %Y")

    if fmt == "time":
        return f"{_strip_leading_zero(dt.strftime('%I:%M'))} {dt.strftime('%p')}"

    if fmt == "datetime":
        if dt.time() == time():
            return dt.strftime("%d %b %Y")
        time_part = f"{_strip_leading_zero(dt.strftime('%I:%M'))} {dt.strftime('%p')}"
        return f"{dt.strftime('%d %b %Y')}, {time_part}"

    return dt.strftime(fmt)


# ---------------------------------------------------------------------------
# Neto control-panel deep links
# ---------------------------------------------------------------------------
# Centralised so we don't sprinkle the base URL across templates. Used by the
# customer 360 card to surface clickable links into the live Neto cpanel.

_NETO_BASE = "https://www.chainsawspares.com.au/_cpanel"


def neto_url(kind: str, id_value) -> str | None:
    """Build a Neto control-panel link for the given resource.

    ``kind`` is one of:
      * ``customer`` — customer profile (id is the Username)
      * ``order``    — order detail (id is the OrderID, e.g. ``JJ617208``)
      * ``rma``      — RMA edit page (id is the RmaID)

    Returns ``None`` when the id is empty so templates can ``{% if %}``-guard.
    """
    if not id_value:
        return None
    paths = {
        "customer": "customer/view",
        "order":    "order/vieworder",
        "rma":      "rma/editrma",
        "product":  "products/view",
    }
    path = paths.get(kind)
    if path is None:
        return None
    return f"{_NETO_BASE}/{path}?id={id_value}"


def register(app) -> None:
    """Wire the filters into a Flask app's Jinja environment."""
    app.add_template_filter(format_dt, name="format_dt")
    app.jinja_env.globals["neto_url"] = neto_url
