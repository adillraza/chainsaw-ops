"""Role model -- stores role -> capabilities mapping in the database.

Capabilities themselves are defined in code (see
``app.auth.capabilities.CAPABILITIES``); this table only stores which
capabilities are currently granted to each role. Admins can edit this via
``/admin/roles``.

Design notes:

* ``name`` is the stable key (matches the string stored in ``user.role``).
  Renaming a role is deliberately not supported from the UI to avoid
  orphaning existing ``user.role`` values; system roles cannot be renamed
  at all.
* ``capabilities`` is serialized as a JSON list on SQLite and PostgreSQL,
  which keeps the schema simple (no join table) and is well within the size
  limits of the capability set we expect to ever have (tens, not thousands).
* ``is_system`` marks the three built-in roles (``admin``, ``retail``,
  ``warehouse``) so the UI can prevent accidental deletion and enforce the
  admin anti-lockout guard.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, false as sa_false
from sqlalchemy.ext.mutable import MutableList

from app.extensions import db


class Role(db.Model):
    __tablename__ = "role"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255))
    # JSON list of capability strings granted to this role.
    capabilities = db.Column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    is_system = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=sa_false(),
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def has(self, capability: str) -> bool:
        return capability in (self.capabilities or [])

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Role {self.name} ({len(self.capabilities or [])} caps)>"
