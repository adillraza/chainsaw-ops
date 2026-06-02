"""Local SQLite mirror of the Neto shipping-config snapshot.

The canonical store is BigQuery ``neto_shipping.*``, but the config changes
only when someone hits "Update". To keep the tab fast we mirror the latest
snapshot (config + usage overlays, fully assembled) into one JSON row here, so
page loads read SQLite instead of querying BigQuery. Refreshed after each
scrape; latest row wins.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class NetoShipMirror(db.Model):
    __tablename__ = "neto_ship_mirror"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.String(40))
    scraped_at = db.Column(db.String(40))
    source = db.Column(db.String(40))
    mirrored_at = db.Column(db.DateTime, default=datetime.utcnow)
    payload = db.Column(db.Text, nullable=False)  # JSON: enriched snap + overlays
