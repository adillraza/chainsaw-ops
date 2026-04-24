"""Application factory for chainsaw-ops.

The single-file ``app.py`` has been split into this package. ``app.py`` at the
project root is now a thin shim that calls :func:`create_app` and runs the
development server, so existing entry points (``run.sh``, ``deploy.sh``, the
systemd unit) keep working unchanged.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

from app.extensions import db, login_manager, migrate  # noqa: E402


def create_app(config_object: str | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    from app.config import Config

    app.config.from_object(config_object or Config)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."

    from app import template_filters
    template_filters.register(app)

    # Expose ``can(cap)`` to every template so gating looks like
    #   {% if can('reviews.flag') %} … {% endif %}
    # This is a thin wrapper over ``current_user.can(cap)``.
    def _can(capability: str) -> bool:
        from flask_login import current_user
        from app.auth.abilities import user_can

        return user_can(current_user, capability)

    app.jinja_env.globals["can"] = _can

    from app.models import user as _user_models  # noqa: F401  (register models)
    from app.models import purchase_orders as _po_models  # noqa: F401
    from app.models import reviews as _review_models  # noqa: F401
    from app.models import annotations as _annotation_models  # noqa: F401
    from app.models import role as _role_models  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id: str):
        from app.models.user import User

        return User.query.get(int(user_id))

    from app.blueprints.auth import auth_bp
    from app.blueprints.admin import admin_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.legacy_api import legacy_api_bp
    from app.blueprints.system_api import system_api_bp
    from app.blueprints.annotations import annotations_bp
    from app.blueprints.purchase_orders import purchase_orders_bp
    from app.blueprints.validation import validation_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(legacy_api_bp)
    app.register_blueprint(system_api_bp)
    app.register_blueprint(annotations_bp)
    app.register_blueprint(purchase_orders_bp)
    app.register_blueprint(validation_bp)

    return app


def bootstrap_database(app: Flask) -> None:
    """Run dev-time bootstrap (admin user, BigQuery review sync).

    Schema migrations are now Alembic's job (``flask db upgrade``). This helper
    only seeds runtime data that the previous monolithic ``app.py`` used to
    create at import time.
    """
    from app.services.startup import (
        create_admin_user,
        sync_reviews_from_bigquery_safe,
    )

    from app.auth.seed import ensure_system_roles

    with app.app_context():
        create_admin_user()
        ensure_system_roles()
        sync_reviews_from_bigquery_safe()
