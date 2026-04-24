"""Custom Jinja filters registered globally on the Flask app.

Centralised so templates can rely on a small set of consistent helpers instead
of inlining ad-hoc strftime calls and `or '—'` fallbacks everywhere.
"""
from __future__ import annotations

from datetime import date, datetime, time

EM_DASH = "—"


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


def register(app) -> None:
    """Wire the filters into a Flask app's Jinja environment."""
    app.add_template_filter(format_dt, name="format_dt")
