"""User-management screens.

Gating: every endpoint requires the ``users.manage`` capability. The
``is_admin`` column on the User model is kept (and synced with the
``admin`` role) for backwards compat with the legacy ``has_admin_access``
helper, but the authoritative source is now the role -> capability table.
"""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.abilities import require_capability
from app.blueprints.admin import admin_bp
from app.extensions import db
from app.models.role import Role
from app.models.user import User


def _all_role_names() -> list[str]:
    """Return every role name (sorted; system roles first, then custom)."""
    return [
        r.name
        for r in Role.query.order_by(Role.is_system.desc(), Role.name).all()
    ]


@admin_bp.route("/admin")
@login_required
@require_capability("users.manage")
def admin():
    users = User.query.all()
    role_names = _all_role_names()
    return render_template("admin.html", users=users, role_names=role_names)


@admin_bp.route("/admin/create_user", methods=["POST"])
@login_required
@require_capability("users.manage")
def create_user():
    username = request.form["username"]
    password = request.form["password"]
    role_name = request.form.get("role", "retail")

    if role_name not in _all_role_names():
        flash("Invalid role selected.", "error")
        return redirect(url_for("admin.admin"))

    if User.query.filter_by(username=username).first():
        flash("Username already exists", "error")
        return redirect(url_for("admin.admin"))

    user = User(username=username, role=role_name, is_admin=(role_name == "admin"))
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    flash(f"User {username} created successfully", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/reset_password/<int:user_id>", methods=["POST"])
@login_required
@require_capability("users.manage")
def reset_password(user_id: int):
    user = User.query.get_or_404(user_id)
    new_password = request.form["new_password"]

    user.set_password(new_password)
    db.session.commit()

    flash(f"Password reset for {user.username}", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/update_role/<int:user_id>", methods=["POST"])
@login_required
@require_capability("users.manage")
def update_user_role(user_id: int):
    user = User.query.get_or_404(user_id)
    new_role = request.form.get("role", "retail")

    if new_role not in _all_role_names():
        flash("Invalid role selected.", "error")
        return redirect(url_for("admin.admin"))

    if user.id == current_user.id and new_role != "admin":
        # Anti-lockout: an admin can't demote themselves (prevents the last
        # admin accidentally locking everyone out of role management).
        flash("You cannot change your own role from admin.", "error")
        return redirect(url_for("admin.admin"))

    user.role = new_role
    user.is_admin = new_role == "admin"
    db.session.commit()

    flash(f"Role updated for {user.username}", "success")
    return redirect(url_for("admin.admin"))
