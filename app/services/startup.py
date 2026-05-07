"""Dev-time bootstrap: seed admin user, sync reviews from BigQuery."""
from __future__ import annotations

import logging
import threading
import time

from app.extensions import db
from app.models.user import User
from app.services.reviews_sync import sync_reviews_from_bigquery

log = logging.getLogger(__name__)


def create_admin_user() -> None:
    admin_user = User.query.filter_by(username="admin").first()
    if not admin_user:
        admin_user = User(username="admin", is_admin=True, role="admin")
        admin_user.set_password("1234")
        db.session.add(admin_user)
        db.session.commit()
        print("Admin user created: username='admin', password='1234'")


def sync_reviews_from_bigquery_safe() -> None:
    try:
        sync_reviews_from_bigquery()
    except Exception as e:
        print(f"Warning: Could not sync reviews on startup: {str(e)}")


def prewarm_graph_token() -> None:
    """Fetch a Microsoft Graph token at app boot in a daemon thread.

    The first card load otherwise pays ~150-300ms for the auth round-trip.
    Backgrounding the fetch means app startup stays fast (~50ms) and
    cards loaded in the first ~30 seconds after boot still see the cold
    path; everything after is warm. Soft-fails on any error.
    """
    def _worker():
        try:
            import sys
            from pathlib import Path
            scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            from email_backfill import get_token  # type: ignore
            from app.services import customer_360_service as svc

            tok = get_token()
            svc._GRAPH_TOKEN["value"] = tok
            svc._GRAPH_TOKEN["expires_at"] = time.time() + 50 * 60
            log.info("graph token pre-warmed at startup")
        except Exception as exc:
            log.warning("graph token pre-warm failed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name="graph-token-prewarm")
    t.start()
