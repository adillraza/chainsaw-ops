"""Per-call sensitivity flag.

When a leader or admin flags a call as "sensitive" (e.g. it contains a
management portion: escalation conversation, supervisor coaching the
caller, candid HR-like discussion) we insert a row keyed on
``session_id``. The presence of a row means *sensitive* — there is no
``is_sensitive`` column. Unflagging deletes the row.

That presence-as-flag design keeps the schema and the toggle endpoint
small. The audit fields (``flagged_by_user_id``, ``flagged_at``,
``reason``) survive as long as the flag does; the moment a leader
unflags, the row is dropped and any "who flagged it last" attribution
is lost. A separate ``call_sensitivity_audit_log`` could record every
toggle if richer audit is needed later — not now.

The flag is consumed by ``customer_360_service.get_call_details`` and
the call-details modal:

* Users WITH ``support.calls.view_sensitive`` see the full bundle plus
  a "Sensitive" badge so they know the call has been gated for other
  roles. They can also use the toggle (if they additionally hold
  ``support.calls.flag_sensitive``) to flag/unflag.
* Users WITHOUT that capability see only the call's basic metadata —
  agent, duration, status — and a banner explaining the rest is
  restricted. The summary, transcription, classifications, sentiment,
  and audio URL are all stripped server-side, not just hidden in the
  template (defense in depth).
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CallSensitivityFlag(db.Model):
    """Marks one call (by session_id) as containing sensitive content.

    Looked up by ``session_id``; the customer 360 service joins this
    table into ``get_call_details`` to populate ``is_sensitive`` on
    the modal's payload.
    """
    __tablename__ = "call_sensitivity_flag"

    id = db.Column(db.Integer, primary_key=True)
    # The CXone contactId / PBX telephony_session_id this flag protects.
    # Unique so a call can only be flagged once at a time; unflag = delete row.
    session_id = db.Column(db.String(120), nullable=False, unique=True, index=True)

    flagged_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Who flagged it. Nullable so older flags survive user deletion,
    # and so the seed/import flow can backfill without a user reference.
    flagged_by_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=True, index=True
    )

    # Optional one-line note describing why the call was flagged
    # (e.g. "escalation to Dallas, HR matter"). Free text, 240 char cap.
    reason = db.Column(db.String(240))

    flagged_by = db.relationship("User", foreign_keys=[flagged_by_user_id])

    @property
    def flagged_at_local(self) -> datetime | None:
        from app.template_filters import utc_to_mel_naive
        return utc_to_mel_naive(self.flagged_at)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<CallSensitivityFlag sess={self.session_id} by={self.flagged_by_user_id}>"
