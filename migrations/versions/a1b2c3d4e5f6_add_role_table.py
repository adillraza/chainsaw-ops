"""add role table and seed system roles

Introduces a ``role`` table that stores the ``name -> capabilities`` mapping
for the capability-based access control system. The three built-in roles
(admin / retail / warehouse) are seeded with defaults that preserve the
existing behavior so the upgrade is behavior-neutral.

Revision ID: a1b2c3d4e5f6
Revises: 8c9e2f3a4d5b
Create Date: 2026-04-22 23:00:00.000000
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op


revision = "a1b2c3d4e5f6"
down_revision = "9d3a8b4c5e6f"
branch_labels = None
depends_on = None


# Kept inline -- not imported from ``app.auth.capabilities`` -- so the
# migration is self-contained and survives future refactors of that module.
_SEED_ROLES: dict[str, tuple[str, list[str]]] = {
    "admin": (
        "Full access. Built-in role; cannot be deleted.",
        [
            "reviews.flag",
            "reviews.retail.view",
            "reviews.retail.close",
            "reviews.warehouse.view",
            "reviews.warehouse.close",
            "reviews.cancel",
            "notes.add",
            "notes.delete_any",
            "stock.view",
            "users.manage",
            "roles.manage",
        ],
    ),
    "retail": (
        "Retail team: runs the retail review workflow.",
        [
            "reviews.flag",
            "reviews.retail.view",
            "reviews.retail.close",
            "reviews.cancel",
            "notes.add",
            "stock.view",
        ],
    ),
    "warehouse": (
        "Warehouse team: responds to retail-flagged reviews.",
        [
            "reviews.warehouse.view",
            "reviews.warehouse.close",
            "notes.add",
            "stock.view",
        ],
    ),
}


def upgrade():
    op.create_table(
        "role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_role_name"),
    )
    with op.batch_alter_table("role", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_role_name"), ["name"], unique=True)

    role_tbl = sa.table(
        "role",
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("capabilities", sa.JSON),
        sa.column("is_system", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )

    # Pass the raw Python list; SQLAlchemy's ``JSON`` type handles
    # serialization for both SQLite and PostgreSQL.
    now = datetime.utcnow()
    payload = [
        {
            "name": name,
            "description": description,
            "capabilities": caps,
            "is_system": True,
            "created_at": now,
            "updated_at": now,
        }
        for name, (description, caps) in _SEED_ROLES.items()
    ]
    op.bulk_insert(role_tbl, payload)


def downgrade():
    with op.batch_alter_table("role", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_role_name"))
    op.drop_table("role")
