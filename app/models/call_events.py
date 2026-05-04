"""Live-call webhook capture model + per-user pinned calls."""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CallEvent(db.Model):
    __tablename__ = "call_event"

    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Where the event came from. We may register multiple webhook URLs over
    # time (RC PBX, CXone, internal test); this lets us split.
    source = db.Column(db.String(40), nullable=False)

    # Best-effort parsed-out fields. NULL for events where we can't pull them.
    event_type = db.Column(db.String(120))
    session_id = db.Column(db.String(120), index=True)
    # CXone ``masterContactId`` (the ID shared across all legs of a transferred
    # call). For non-transfer calls it equals ``session_id``; for PBX events
    # it's NULL because ``telephony_session_id`` already aggregates legs.
    master_session_id = db.Column(db.String(120), index=True)
    from_number = db.Column(db.String(50), index=True)
    to_number = db.Column(db.String(50))

    # Verbatim capture for forensic inspection / future re-parsing.
    headers_json = db.Column(db.Text)
    body_json = db.Column(db.Text, nullable=False)

    def __repr__(self) -> str:
        return f"<CallEvent {self.id} {self.event_type or '(no type)'} {self.from_number or ''}>"


class PinnedCall(db.Model):
    """A call any agent has pinned for follow-up.

    Pins are **shared across the whole team** — anyone can pin/unpin, and
    everyone sees the same Pin Calls list. ``pinned_by_user_id`` is kept
    for display attribution ("pinned by X") but not for access control.

    Snapshots the call's display fields (including the resolved customer
    name) at pin time so the pin keeps rendering even after the source
    ``call_event`` rows are pruned or rolled into BigQuery.
    """
    __tablename__ = "pinned_call"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    pinned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Who pinned it (display only, nullable so anonymous / system pins work)
    pinned_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    # Snapshotted at pin time
    phone         = db.Column(db.String(50))
    to_number     = db.Column(db.String(50))
    direction     = db.Column(db.String(20))
    status_at_pin = db.Column(db.String(120))
    source        = db.Column(db.String(40))
    agent_name    = db.Column(db.String(120))
    skill         = db.Column(db.String(120))
    customer_name = db.Column(db.String(200))
    note          = db.Column(db.Text)

    pinned_by = db.relationship("User", backref="pinned_calls", foreign_keys=[pinned_by_user_id])

    @property
    def pinned_at_local(self) -> datetime | None:
        """Mel-local naive view of ``pinned_at`` for templates.

        ``pinned_at`` is naive UTC (default ``datetime.utcnow``); the
        format_dt template filter assumes "naive = already Mel", so
        templates should use this property to display.
        """
        from app.template_filters import utc_to_mel_naive
        return utc_to_mel_naive(self.pinned_at)

    def __repr__(self) -> str:
        return f"<PinnedCall sess={self.session_id} by={self.pinned_by_user_id}>"
