"""Item-review (warehouse / retail) workflow model."""
from __future__ import annotations

from datetime import datetime

from app.extensions import db

OPEN_REVIEW_STATUSES = ["pending", "warehouse_in_progress"]
CLOSED_REVIEW_STATUSES = ["warehouse_closed", "retail_closed", "cancelled"]


class ItemReview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    po_id = db.Column(db.String(50), nullable=False, index=True)
    order_id = db.Column(db.String(50))
    po_item_id = db.Column(db.String(50), index=True)
    sku = db.Column(db.String(100))
    flagged_by = db.Column(db.String(100), nullable=False)
    flagged_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    flag_comment = db.Column(db.Text)
    status = db.Column(db.String(50), default="pending", index=True)
    warehouse_assigned_to = db.Column(db.String(100))
    warehouse_started_at = db.Column(db.DateTime)
    warehouse_comment = db.Column(db.Text)
    warehouse_closed_at = db.Column(db.DateTime)
    retail_closed_by = db.Column(db.String(100))
    retail_closed_at = db.Column(db.DateTime)
    retail_comment = db.Column(db.Text)
    comparison_snapshot = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "po_id": self.po_id,
            "order_id": self.order_id,
            "po_item_id": self.po_item_id,
            "sku": self.sku,
            "flagged_by": self.flagged_by,
            "flagged_at": self.flagged_at.isoformat() if self.flagged_at else None,
            "flag_comment": self.flag_comment,
            "status": self.status,
            "warehouse_assigned_to": self.warehouse_assigned_to,
            "warehouse_started_at": self.warehouse_started_at.isoformat() if self.warehouse_started_at else None,
            "warehouse_comment": self.warehouse_comment,
            "warehouse_closed_at": self.warehouse_closed_at.isoformat() if self.warehouse_closed_at else None,
            "retail_closed_by": self.retail_closed_by,
            "retail_closed_at": self.retail_closed_at.isoformat() if self.retail_closed_at else None,
            "retail_comment": self.retail_comment,
            "comparison_snapshot": self.comparison_snapshot,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
