"""Capability catalog.

A *capability* is a short, dot-separated action string (e.g. ``reviews.flag``)
that a role can be granted. Capabilities are defined in code because they
correspond to real code paths -- you can't grant a capability that has no
implementation. Roles are defined in the database (see ``app.models.role``)
so an admin can create/edit/delete roles and their capability assignments
without a code change.

Rule of thumb: **one capability per meaningful action that we gate on.**
Don't create a "view" capability unless the section is actually guarded on
access; don't create "write" vs "read" capabilities (we intentionally use
"if you can see it, you can do it" for this product).

Every call site in routes/templates MUST go through ``user.can(cap)`` or
``require_capability(cap)``. Nothing should read ``user.role`` directly
except for display (e.g. showing the role name on the user's avatar).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
# Grouped purely for display in the admin Roles UI. Flattening into a set
# happens below.

CAPABILITY_GROUPS: dict[str, list[tuple[str, str]]] = {
    "PO Cross Check – Reviews": [
        ("reviews.flag",             "Flag an item on the Comparison page"),
        ("reviews.retail.view",      "See the Retail Reviews queue"),
        ("reviews.retail.close",     "Close a review as retail"),
        ("reviews.warehouse.view",   "See the Warehouse Reviews queue"),
        ("reviews.warehouse.close",  "Respond/close a review as warehouse"),
        ("reviews.cancel",           "Cancel an in-flight review"),
    ],
    "PO Cross Check – Notes": [
        ("notes.add",                "Add notes to POs and items"),
        ("notes.delete_any",         "Delete any user's note (not just your own)"),
    ],
    "Validation": [
        ("validation.view",          "See the Validation section"),
        ("validation.msl.approve",   "Approve MSL (minimum stock level) changes"),
    ],
    "Administration": [
        ("users.manage",             "Create users, reset passwords, assign roles"),
        ("roles.manage",             "Create, edit, and delete roles and their capabilities"),
    ],
}

# Flat set used for validation.
CAPABILITIES: frozenset[str] = frozenset(
    cap
    for group in CAPABILITY_GROUPS.values()
    for cap, _ in group
)


def is_valid_capability(cap: str) -> bool:
    """Return True when ``cap`` is a known capability string."""
    return cap in CAPABILITIES


# ---------------------------------------------------------------------------
# System role defaults
# ---------------------------------------------------------------------------
# These are the capability sets seeded for the three built-in roles on first
# startup (and used by the Alembic migration). Admins can edit them at
# runtime via the admin UI; we never re-assert these defaults after seeding.
#
# ``admin`` is special: it is granted every capability, always. The DB row
# for ``admin`` is also flagged as a system role (``is_system=True``) so the
# UI prevents deletion and prevents stripping ``users.manage`` /
# ``roles.manage`` from it (anti-lockout guard enforced server-side).

SYSTEM_ROLE_DEFAULTS: dict[str, set[str]] = {
    "admin": set(CAPABILITIES),
    "retail": {
        "reviews.flag",
        "reviews.retail.view",
        "reviews.retail.close",
        "reviews.cancel",
        "notes.add",
    },
    "warehouse": {
        "reviews.warehouse.view",
        "reviews.warehouse.close",
        "notes.add",
    },
}

# Roles that must exist and cannot be deleted via the UI. Their capability
# lists can still be edited (except where the anti-lockout guard kicks in).
SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(SYSTEM_ROLE_DEFAULTS)

# Capabilities the ``admin`` role must always keep, to prevent locking out
# all administration (see ``app.blueprints.admin.role_routes``).
ADMIN_PROTECTED_CAPABILITIES: frozenset[str] = frozenset({
    "users.manage",
    "roles.manage",
})
