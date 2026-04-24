"""Idempotent seeder for the three system roles.

Runs at app bootstrap so a fresh dev database (or any environment that
loses the Alembic-seeded rows) still gets a working set of roles without
a manual migration.

Rules:

* **Missing system roles** are created with their defaults.
* **admin** is always kept in sync with the full capability catalog --
  any capability added in code is auto-granted to admin on the next
  restart. This preserves the "admin can do everything" invariant across
  upgrades that introduce new features.
* **retail / warehouse** are created with defaults ONCE; after that we
  never overwrite their capability lists, so admin customizations via
  the UI are preserved.
* All system roles are re-marked ``is_system=True`` defensively.
"""
from __future__ import annotations

from app.auth.capabilities import CAPABILITIES, SYSTEM_ROLE_DEFAULTS
from app.extensions import db
from app.models.role import Role


def ensure_system_roles() -> None:
    changed = False
    existing = {r.name: r for r in Role.query.filter(Role.name.in_(SYSTEM_ROLE_DEFAULTS)).all()}

    for name, caps in SYSTEM_ROLE_DEFAULTS.items():
        role = existing.get(name)
        if role is None:
            role = Role(
                name=name,
                description=f"Built-in {name} role.",
                capabilities=sorted(caps),
                is_system=True,
            )
            db.session.add(role)
            changed = True
            print(f"Seeded system role: {name}")
            continue

        if not role.is_system:
            role.is_system = True
            changed = True

        # Top up the admin role whenever new capabilities are added to the
        # catalog -- admin should always own the complete set.
        if name == "admin":
            current = set(role.capabilities or [])
            full = set(CAPABILITIES)
            if current != full:
                role.capabilities = sorted(full)
                changed = True
                added = full - current
                removed = current - full
                if added:
                    print(f"admin role: granted new capabilities {sorted(added)}")
                if removed:
                    print(f"admin role: pruned retired capabilities {sorted(removed)}")

    if changed:
        db.session.commit()
        from app.auth.abilities import invalidate_cache

        invalidate_cache()
