"""Live-call webhook capture model.

One row per RingCentral webhook delivery. v1 stores the raw body and headers
so we can inspect real payload shapes; typed columns are populated where the
event clearly carries them (session_id, from_number, etc.) but stay nullable
for events that don't.
"""
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
