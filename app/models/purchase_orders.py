"""Local cache of BigQuery purchase-order tables.

The cache is rebuilt by :mod:`app.services.cache` on demand, and the previous
``app.py`` had three near-identical schemas — preserved here verbatim so the
existing SQLite database keeps working.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CachedPurchaseOrderSummary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False, index=True)
    po_status = db.Column(db.String(50))
    rex_po_created_by = db.Column(db.String(100))
    received_by = db.Column(db.String(100))
    supplier = db.Column(db.String(150))
    requested_date = db.Column(db.DateTime)
    order_id = db.Column(db.String(50), index=True)
    order_link = db.Column(db.String(500))
    entered_date = db.Column(db.DateTime)
    received_date = db.Column(db.DateTime)
    neto_order_created_by = db.Column(db.String(100))
    completed_date = db.Column(db.DateTime)
    completion_status = db.Column(db.String(50))
    order_status = db.Column(db.String(50))
    difference = db.Column(db.Float)
    disparity = db.Column(db.Boolean)
    item_count = db.Column(db.Integer)
    total_quantity_ordered = db.Column(db.Float)
    total_quantity_received = db.Column(db.Float)
    total_rex_cost = db.Column(db.Float)
    total_neto_cost = db.Column(db.Float)
    latest_po_note = db.Column(db.Text)
    latest_po_note_user = db.Column(db.String(100))
    latest_po_note_date = db.Column(db.DateTime)
    no_of_neto_orders = db.Column(db.Integer)
    neto_order_ids = db.Column(db.Text)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "po_id": self.po_id,
            "po_status": self.po_status,
            "rex_po_created_by": self.rex_po_created_by,
            "received_by": self.received_by,
            "supplier": self.supplier,
            "requested_date": self.requested_date.isoformat() if self.requested_date else None,
            "OrderID": self.order_id,
            "order_link": self.order_link,
            "entered_date": self.entered_date.isoformat() if self.entered_date else None,
            "received_date": self.received_date.isoformat() if self.received_date else None,
            "neto_order_created_by": self.neto_order_created_by,
            "completed_date": self.completed_date.isoformat() if self.completed_date else None,
            "completion_status": self.completion_status,
            "order_status": self.order_status,
            "difference": self.difference,
            "disparity": self.disparity,
            "item_count": self.item_count,
            "total_quantity_ordered": self.total_quantity_ordered,
            "total_quantity_received": self.total_quantity_received,
            "total_rex_cost": self.total_rex_cost,
            "total_neto_cost": self.total_neto_cost,
            "latest_po_note": self.latest_po_note,
            "latest_po_note_user": self.latest_po_note_user,
            "latest_po_note_date": self.latest_po_note_date.isoformat() if self.latest_po_note_date else None,
            "no_of_neto_orders": self.no_of_neto_orders,
            "neto_order_ids": self.neto_order_ids,
        }


class CachedPurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False, index=True)
    po_item_id = db.Column(db.String(50), index=True)
    sku = db.Column(db.String(100), index=True)
    supplier_sku = db.Column(db.String(100))
    manufacturer_sku = db.Column(db.String(100))
    short_description = db.Column(db.String(500))
    neto_qty_ordered = db.Column(db.Integer)
    rex_qty_ordered = db.Column(db.Integer)
    rex_qty_received = db.Column(db.Integer)
    neto_qty_available = db.Column(db.String(50))
    neto_cost_price = db.Column(db.Float)
    rex_supplier_buy_ex = db.Column(db.Float)
    difference = db.Column(db.Float)
    disparity = db.Column(db.Boolean)
    order_id = db.Column(db.String(50), index=True)
    created_on = db.Column(db.DateTime)
    modified_on = db.Column(db.DateTime)
    latest_item_note = db.Column(db.Text)
    latest_item_note_user = db.Column(db.String(100))
    latest_item_note_date = db.Column(db.DateTime)
    # Neto product ID (from netocssv2.Products via dataform.neto_product_list).
    # Stored as text so we can keep BigQuery's 64-bit IDs intact and just paste
    # them into the Neto cpanel deep link.
    neto_product_id = db.Column(db.String(50), index=True)
    # True when the SKU is a kit/bundle in NETO. Kitted items shouldn't appear
    # on a PO at all, so we badge them in the UI as an anomaly.
    is_kitted_item = db.Column(db.Boolean, index=True)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "po_id": self.po_id,
            "po_item_id": self.po_item_id,
            "sku": self.sku,
            "supplier_sku": self.supplier_sku,
            "manufacturer_sku": self.manufacturer_sku,
            "short_description": self.short_description,
            "neto_qty_ordered": self.neto_qty_ordered,
            "rex_qty_ordered": self.rex_qty_ordered,
            "rex_qty_received": self.rex_qty_received,
            "neto_qty_available": self.neto_qty_available,
            "neto_cost_price": self.neto_cost_price,
            "rex_supplier_buy_ex": self.rex_supplier_buy_ex,
            "difference": self.difference,
            "disparity": self.disparity,
            "OrderID": self.order_id,
            "created_on": self.created_on.isoformat() if self.created_on else None,
            "modified_on": self.modified_on.isoformat() if self.modified_on else None,
            "latest_item_note": self.latest_item_note,
            "latest_item_note_user": self.latest_item_note_user,
            "latest_item_note_date": self.latest_item_note_date.isoformat() if self.latest_item_note_date else None,
            "neto_product_id": self.neto_product_id,
            "is_kitted_item": self.is_kitted_item,
        }


class CachedPurchaseOrderComparison(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.String(50), nullable=False, index=True)
    modified_on = db.Column(db.DateTime)
    sku = db.Column(db.String(100), index=True)
    name = db.Column(db.String(500))
    change_log = db.Column(db.String(100))
    rex_available_qty = db.Column(db.Float)
    neto_qty_available = db.Column(db.Float)
    original_rex_qty_ordered = db.Column(db.Float)
    neto_qty_shipped = db.Column(db.Float)
    final_rex_qty_ordered = db.Column(db.Float)
    rex_qty_received = db.Column(db.Float)
    order_id = db.Column(db.String(50), index=True)
    po_item_id = db.Column(db.String(50), index=True)
    latest_item_note = db.Column(db.Text)
    latest_item_note_user = db.Column(db.String(100))
    latest_item_note_date = db.Column(db.DateTime)
    # See ``CachedPurchaseOrderItem.neto_product_id`` for context.
    neto_product_id = db.Column(db.String(50), index=True)
    # Mirrors ``CachedPurchaseOrderItem.is_kitted_item``.
    is_kitted_item = db.Column(db.Boolean, index=True)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "po_id": self.po_id,
            "modified_on": self.modified_on.isoformat() if self.modified_on else None,
            "sku": self.sku,
            "name": self.name,
            "change_log": self.change_log,
            "rex_available_qty": self.rex_available_qty,
            "neto_qty_available": self.neto_qty_available,
            "original_rex_qty_ordered": self.original_rex_qty_ordered,
            "neto_qty_shipped": self.neto_qty_shipped,
            "final_rex_qty_ordered": self.final_rex_qty_ordered,
            "rex_qty_received": self.rex_qty_received,
            "OrderID": self.order_id,
            "po_item_id": self.po_item_id,
            "latest_item_note": self.latest_item_note,
            "latest_item_note_user": self.latest_item_note_user,
            "latest_item_note_date": self.latest_item_note_date.isoformat() if self.latest_item_note_date else None,
            "neto_product_id": self.neto_product_id,
            "is_kitted_item": self.is_kitted_item,
        }
