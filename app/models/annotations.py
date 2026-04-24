"""Generic notes / comments attached to any entity in the system.

Phase-1.5 introduces this model so future sections (Stock & Inventory, etc.)
can support notes "for free" without growing yet another bespoke ``*_notes``
table. Existing PO and item notes (BigQuery-backed) are unaffected.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from app.extensions import db


def _new_uuid() -> str:
    return uuid.uuid4().hex


class Annotation(db.Model):
    __tablename__ = "annotation"

    id = db.Column(db.String(32), primary_key=True, default=_new_uuid)
    entity_type = db.Column(db.String(50), nullable=False, index=True)
    entity_id = db.Column(db.String(100), nullable=False, index=True)
    parent_id = db.Column(db.String(32), db.ForeignKey("annotation.id"), nullable=True, index=True)
    comment = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    author_username = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    extra = db.Column("meta", db.JSON, nullable=True)

    author = db.relationship("User", lazy="joined")
    children = db.relationship("Annotation", backref=db.backref("parent", remote_side="Annotation.id"))

    __table_args__ = (
        db.Index("ix_annotation_entity", "entity_type", "entity_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "parent_id": self.parent_id,
            "comment": self.comment,
            "author_id": self.author_id,
            "author_username": self.author_username,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "meta": self.extra,
        }
