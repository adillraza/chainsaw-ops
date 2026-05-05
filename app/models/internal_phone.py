"""Registry of JJ-internal phone numbers.

Pulled from the RingCentral ``account/~/phone-number`` endpoint and seeded
via migration ``f4a5b6c7d8e9_add_internal_phone_numbers``. See
``docs/internal-phone-numbers.md`` for the full list with owners.

These numbers belong to JJ — IVR DIDs, main company lines, staff direct
extensions, fax. They should NEVER be treated as customer phone
numbers. The Customer 360 service checks this table before rendering
a customer card; the call-history pipeline filters them out so they
don't pollute per-customer call counts.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class InternalPhoneNumber(db.Model):
    __tablename__ = "internal_phone_numbers"

    # AU-local form, no country code (matches the convention used everywhere
    # else in the app — call_event.from_number is normalised to this shape).
    phone = db.Column(db.String(20), primary_key=True)

    # E.164 (+61…) — kept alongside for diagnostic/lookup convenience.
    e164 = db.Column(db.String(20), nullable=False)

    # RC-classifier strings: ContactCenterNumber / MainCompanyNumber /
    # CompanyNumber / DirectNumber / CompanyFaxNumber.
    usage_type = db.Column(db.String(40))

    # Free-text label — extension owner ("Belinda Battistin"), or a
    # human-readable name for impersonal lines ("Customer Service IB").
    label = db.Column(db.String(120))

    # RC extension number when applicable (1002, 2003, etc).
    extension_number = db.Column(db.String(20))

    # When this row was last refreshed from the RC API.
    synced_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<InternalPhoneNumber {self.phone} {self.label!r}>"
