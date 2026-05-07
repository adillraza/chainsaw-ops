"""add cache_watermark table

Revision ID: 3aabbccddeef
Revises: 21eea38ca7fb
Create Date: 2026-05-07 14:30:00.000000

Per-table sync watermarks for incremental cache refresh. The
``customer_360`` Phase 2 incremental loader writes here after each run
so the next run can pull only rows changed since.
"""
from alembic import op
import sqlalchemy as sa


revision = '3aabbccddeef'
down_revision = '21eea38ca7fb'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cache_watermark',
        sa.Column('cache_name', sa.String(length=50), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(), nullable=False),
        sa.Column('rows_last_run', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('cache_name'),
    )


def downgrade():
    op.drop_table('cache_watermark')
