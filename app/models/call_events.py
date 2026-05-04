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
    from_number = db.Column(db.String(50), index=True)
    to_number = db.Column(db.String(50))

    # Verbatim capture for forensic inspection / future re-parsing.
    headers_json = db.Column(db.Text)
    body_json = db.Column(db.Text, nullable=False)

    def __repr__(self) -> str:
        return f"<CallEvent {self.id} {self.event_type or '(no type)'} {self.from_number or ''}>"


class PinnedCall(db.Model):
    """A call an agent has pinned for follow-up.

    Snapshots the call's display fields at pin time so the pin survives
    after the source ``call_event`` rows are pruned or migrated into BQ.
    """
    __tablename__ = "pinned_call"
    __table_args__ = (
        db.UniqueConstraint("user_id", "session_id", name="uq_pinned_call_user_session"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    session_id = db.Column(db.String(120), nullable=False, index=True)
    pinned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Snapshotted at pin time
    phone         = db.Column(db.String(50))
    to_number     = db.Column(db.String(50))
    direction     = db.Column(db.String(20))
    status_at_pin = db.Column(db.String(120))
    source        = db.Column(db.String(40))
    agent_name    = db.Column(db.String(120))
    skill         = db.Column(db.String(120))
    note          = db.Column(db.Text)

    user = db.relationship("User", backref="pinned_calls")

    def __repr__(self) -> str:
        return f"<PinnedCall user={self.user_id} sess={self.session_id}>"
