"""Add pinned_call table for the live-calls drawer Pin Calls section.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-04 12:50:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    """Team-shared pinned calls.

    Pins are global — anyone on the team can pin or unpin, and everyone
    sees the same list. ``pinned_by_user_id`` is kept for display
    attribution but not for access control. Each ``session_id`` can be
    pinned at most once (re-pinning is a no-op).

    Display fields are snapshotted at pin time so the pin keeps rendering
    after the source ``call_event`` rows are pruned or rolled into BigQuery.
    """
    # SQLite can't ALTER TABLE ADD CONSTRAINT, so the unique constraint
    # has to be inlined into CREATE TABLE rather than added afterwards.
    op.create_table(
        'pinned_call',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.String(length=120), nullable=False, unique=True),
        sa.Column('pinned_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        # Display attribution only — nullable so the column can be added
        # without a backfill if we ever pin from a system/poller context.
        sa.Column('pinned_by_user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
        # Snapshotted at pin time so the pin keeps rendering after the
        # source call_event rows are pruned or rolled into BigQuery.
        sa.Column('phone',          sa.String(length=50),  nullable=True),
        sa.Column('to_number',      sa.String(length=50),  nullable=True),
        sa.Column('direction',      sa.String(length=20),  nullable=True),
        sa.Column('status_at_pin',  sa.String(length=120), nullable=True),
        sa.Column('source',         sa.String(length=40),  nullable=True),
        sa.Column('agent_name',     sa.String(length=120), nullable=True),
        sa.Column('skill',          sa.String(length=120), nullable=True),
        sa.Column('customer_name',  sa.String(length=200), nullable=True),
        sa.Column('note',           sa.Text(),             nullable=True),
    )
    op.create_index('ix_pinned_call_session_id',         'pinned_call', ['session_id'])
    op.create_index('ix_pinned_call_pinned_by_user_id',  'pinned_call', ['pinned_by_user_id'])


def downgrade():
    op.drop_index('ix_pinned_call_pinned_by_user_id', table_name='pinned_call')
    op.drop_index('ix_pinned_call_session_id',        table_name='pinned_call')
    op.drop_table('pinned_call')
