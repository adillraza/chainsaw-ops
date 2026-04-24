"""Login / logout."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.extensions import db
from app.models.user import LoginLog, User


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            remember_me = request.form.get("remember_me") == "on"
            login_user(user, remember=remember_me)

            login_log = LoginLog(
                user_id=user.id,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent"),
            )
            db.session.add(login_log)
            db.session.commit()

            flash("Login successful! Refreshing data in background...", "success")
            return redirect(url_for("dashboard.dashboard"))
        else:
            flash("Invalid username or password", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out", "info")
    return redirect(url_for("auth.login"))
