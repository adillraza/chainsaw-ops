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
    """Per-user pinned calls.

    Pinning a call snapshots the relevant fields (phone, agent, etc.) so the
    pin survives even after the source ``call_event`` rows have been pruned.
    Each (user, session_id) is unique — pinning the same session twice
    silently no-ops, unpinning is by session_id under the same user.
    """
    # SQLite can't ALTER TABLE ADD CONSTRAINT, so the unique constraint
    # has to be inlined into CREATE TABLE rather than added afterwards.
    op.create_table(
        'pinned_call',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('session_id', sa.String(length=120), nullable=False),
        sa.Column('pinned_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        # Snapshotted at pin time so the pin keeps rendering after the source
        # call_event rows are pruned or rolled into BigQuery.
        sa.Column('phone',          sa.String(length=50),  nullable=True),
        sa.Column('to_number',      sa.String(length=50),  nullable=True),
        sa.Column('direction',      sa.String(length=20),  nullable=True),
        sa.Column('status_at_pin',  sa.String(length=120), nullable=True),
        sa.Column('source',         sa.String(length=40),  nullable=True),
        sa.Column('agent_name',     sa.String(length=120), nullable=True),
        sa.Column('skill',          sa.String(length=120), nullable=True),
        sa.Column('note',           sa.Text(),             nullable=True),
        sa.UniqueConstraint('user_id', 'session_id', name='uq_pinned_call_user_session'),
    )
    op.create_index('ix_pinned_call_user_id',    'pinned_call', ['user_id'])
    op.create_index('ix_pinned_call_session_id', 'pinned_call', ['session_id'])


def downgrade():
    op.drop_index('ix_pinned_call_session_id', table_name='pinned_call')
    op.drop_index('ix_pinned_call_user_id',    table_name='pinned_call')
    op.drop_table('pinned_call')
