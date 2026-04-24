"""Auth and login-tracking models."""
from __future__ import annotations

from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import true as sa_true
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    role = db.Column(db.String(40), default="retail", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    prefers_v2 = db.Column(db.Boolean, default=True, nullable=False, server_default=sa_true())

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def can(self, capability: str) -> bool:
        """True when this user's role grants ``capability``.

        Thin wrapper over :func:`app.auth.abilities.user_can` so templates
        and routes can write ``current_user.can("reviews.flag")`` without
        an import.
        """
        from app.auth.abilities import user_can

        return user_can(self, capability)

    @property
    def capabilities(self) -> frozenset[str]:
        """Return every capability this user currently has via their role."""
        from app.auth.abilities import capabilities_for

        return capabilities_for(self.role)


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    login_time = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))

    user = db.relationship("User", backref="login_logs")
