"""Hourly delta sync for sales@ → BigQuery.

Designed to be the recurring counterpart to email_backfill.py — it
reuses the exact same per-folder delta-walk machinery, just resuming
from each folder's saved deltaLink instead of starting fresh.

Run as a systemd timer every hour:

    [Unit]
    Description=Sync sales@ mailbox into BigQuery (hourly)

    [Service]
    Type=oneshot
    ExecStart=/usr/bin/python3 /opt/chainsaw-ops/scripts/email_sync.py
    User=chainsaw-ops

    [Timer]
    OnCalendar=hourly
    Persistent=true

A normal run after backfill is small — typically <100 new messages,
takes a few seconds. The same script doubles as the backfill resumer
if a previous run crashed midway through a folder; it just picks up
where the deltaLink was last saved.

For ad-hoc "pull recent N messages" without delta machinery, use
email_pull_recent.py — that's what the customer 360 panel calls when
an agent loads a card and wants the freshest possible view.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_backfill import run

if __name__ == "__main__":
    # No args needed — every folder resumes from its saved deltaLink.
    # If a folder has no deltaLink (e.g. brand-new folder created since
    # backfill), it'll do a full walk of just that folder, which is fine.
    run(limit=None, reset=False)
