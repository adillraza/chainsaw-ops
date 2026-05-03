"""Add call_event table for RingCentral webhook capture.

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-05-04 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c1d2e3f4a5b6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    """One row per RingCentral webhook delivery — raw capture, no parsing yet.

    The blueprint stores the headers and JSON body verbatim so we can inspect
    real payload shapes once real traffic starts flowing. Once the shape is
    understood, we'll add typed columns (session_id, from_number, etc.) in a
    follow-up migration.
    """
    op.create_table(
        'call_event',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('received_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('source', sa.String(length=40), nullable=False),  # 'ringcentral_pbx', 'cxone', 'test', etc.
        sa.Column('event_type', sa.String(length=120), nullable=True),
        sa.Column('session_id', sa.String(length=120), nullable=True),
        sa.Column('from_number', sa.String(length=50), nullable=True),
        sa.Column('to_number', sa.String(length=50), nullable=True),
        sa.Column('headers_json', sa.Text(), nullable=True),
        sa.Column('body_json', sa.Text(), nullable=False),
    )
    op.create_index('ix_call_event_received_at', 'call_event', ['received_at'])
    op.create_index('ix_call_event_session_id',  'call_event', ['session_id'])
    op.create_index('ix_call_event_from_number', 'call_event', ['from_number'])


def downgrade():
    op.drop_index('ix_call_event_from_number', table_name='call_event')
    op.drop_index('ix_call_event_session_id',  table_name='call_event')
    op.drop_index('ix_call_event_received_at', table_name='call_event')
    op.drop_table('call_event')
