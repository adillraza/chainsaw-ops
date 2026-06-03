"""Local SQLite mirror of the Neto Advanced-Configuration snapshot.

Canonical store is BigQuery ``neto_config.*``; config changes only when someone
hits "Update". To keep the tab fast we mirror the latest snapshot (fully
assembled) into one JSON row here so page loads read SQLite, not BigQuery.
Latest row wins.
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db


class NetoConfigMirror(db.Model):
    __tablename__ = "neto_config_mirror"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.String(40))
    scraped_at = db.Column(db.String(40))
    source = db.Column(db.String(40))
    mirrored_at = db.Column(db.DateTime, default=datetime.utcnow)
    payload = db.Column(db.Text, nullable=False)  # JSON: assembled snapshot
