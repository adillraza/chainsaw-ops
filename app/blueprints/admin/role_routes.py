"""Admin UI for managing roles and their capability assignments.

Every endpoint here is gated on the ``roles.manage`` capability so only
admins hit it. The routes follow a classic server-rendered pattern
(``GET`` lists/forms, ``POST`` mutations with a flash + redirect) to keep
parity with the surrounding admin pages.

Guard rails enforced server-side:

* **System roles** (``admin``/``retail``/``warehouse``) cannot be deleted
  and cannot be renamed. Their capabilities can be edited except for the
  two admin anti-lockout caps below.
* The **admin** role must always keep ``users.manage`` and
  ``roles.manage`` -- otherwise nobody could re-grant them.
* Role names must be unique, lowercase-ish (``^[a-z0-9_-]+$``), and the
  UI catalogue restricts every POSTed capability to a known value.
"""
from __future__ import annotations

import re

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from app.auth.abilities import invalidate_cache, require_capability
from app.auth.capabilities import (
    ADMIN_PROTECTED_CAPABILITIES,
    CAPABILITIES,
    CAPABILITY_GROUPS,
    SYSTEM_ROLE_NAMES,
)
from app.blueprints.admin import admin_bp
from app.extensions import db
from app.models.role import Role
from app.models.user import User


_ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


def _parse_posted_capabilities() -> list[str]:
    """Return the capability strings from the form, filtered to the catalog."""
    posted = set(request.form.getlist("capabilities"))
    return sorted(cap for cap in posted if cap in CAPABILITIES)


def _role_user_counts() -> dict[str, int]:
    """Return ``{role_name: user_count}`` for the role list view."""
    rows = (
        db.session.query(User.role, func.count(User.id))
        .group_by(User.role)
        .all()
    )
    return {name: count for name, count in rows}


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@admin_bp.route("/admin/roles")
@login_required
@require_capability("roles.manage")
def roles_list():
    roles = Role.query.order_by(Role.is_system.desc(), Role.name).all()
    counts = _role_user_counts()
    return render_template(
        "admin_roles_list.html",
        roles=roles,
        user_counts=counts,
        capability_groups=CAPABILITY_GROUPS,
    )


@admin_bp.route("/admin/roles/create", methods=["POST"])
@login_required
@require_capability("roles.manage")
def roles_create():
    name = (request.form.get("name") or "").strip().lower()
    description = (request.form.get("description") or "").strip() or None
    caps = _parse_posted_capabilities()

    if not _ROLE_NAME_RE.match(name):
        flash(
            "Role name must start with a letter, be 2–40 chars, and contain "
            "only lowercase letters, digits, dashes, or underscores.",
            "error",
        )
        return redirect(url_for("admin.roles_list"))

    if Role.query.filter_by(name=name).first():
        flash(f"A role named '{name}' already exists.", "error")
        return redirect(url_for("admin.roles_list"))

    role = Role(name=name, description=description, capabilities=caps, is_system=False)
    db.session.add(role)
    db.session.commit()
    invalidate_cache()
    flash(f"Role '{name}' created with {len(caps)} capabilit{'y' if len(caps)==1 else 'ies'}.", "success")
    return redirect(url_for("admin.roles_edit", role_id=role.id))


# ---------------------------------------------------------------------------
# Edit capability grid
# ---------------------------------------------------------------------------


@admin_bp.route("/admin/roles/<int:role_id>/edit", methods=["GET"])
@login_required
@require_capability("roles.manage")
def roles_edit(role_id: int):
    role = Role.query.get_or_404(role_id)
    return render_template(
        "admin_roles_edit.html",
        role=role,
        capability_groups=CAPABILITY_GROUPS,
        admin_protected=ADMIN_PROTECTED_CAPABILITIES,
    )


@admin_bp.route("/admin/roles/<int:role_id>/update", methods=["POST"])
@login_required
@require_capability("roles.manage")
def roles_update(role_id: int):
    role = Role.query.get_or_404(role_id)
    caps = set(_parse_posted_capabilities())

    # Anti-lockout: the admin role must always keep users.manage and
    # roles.manage, otherwise no one can re-grant them.
    if role.name == "admin":
        missing = ADMIN_PROTECTED_CAPABILITIES - caps
        if missing:
            flash(
                f"The admin role must always keep: {', '.join(sorted(missing))}. "
                "Re-selected automatically.",
                "error",
            )
            caps |= ADMIN_PROTECTED_CAPABILITIES

    # Description is free-text for all roles.
    description = (request.form.get("description") or "").strip() or None

    role.capabilities = sorted(caps)
    role.description = description
    db.session.commit()
    invalidate_cache()

    flash(f"Role '{role.name}' updated.", "success")
    return redirect(url_for("admin.roles_edit", role_id=role.id))


# ---------------------------------------------------------------------------
# Delete (non-system only)
# ---------------------------------------------------------------------------


@admin_bp.route("/admin/roles/<int:role_id>/delete", methods=["POST"])
@login_required
@require_capability("roles.manage")
def roles_delete(role_id: int):
    role = Role.query.get_or_404(role_id)

    if role.is_system or role.name in SYSTEM_ROLE_NAMES:
        flash(f"'{role.name}' is a system role and cannot be deleted.", "error")
        return redirect(url_for("admin.roles_list"))

    in_use = User.query.filter_by(role=role.name).count()
    if in_use:
        flash(
            f"Cannot delete '{role.name}': {in_use} user(s) still hold this role. "
            "Reassign them first.",
            "error",
        )
        return redirect(url_for("admin.roles_list"))

    db.session.delete(role)
    db.session.commit()
    invalidate_cache()
    flash(f"Role '{role.name}' deleted.", "success")
    return redirect(url_for("admin.roles_list"))
