"""Dev-time bootstrap: seed admin user, sync reviews from BigQuery."""
from __future__ import annotations

from app.extensions import db
from app.models.user import User
from app.services.reviews_sync import sync_reviews_from_bigquery


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
