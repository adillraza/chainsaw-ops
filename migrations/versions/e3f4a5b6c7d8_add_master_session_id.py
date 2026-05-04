"""Add master_session_id to call_event for transfer-aware grouping.

CXone creates a separate ``contactId`` for each agent leg of a call.
A warm transfer between two agents shows up as TWO ``contactId`` rows
sharing the same ``masterContactId``. The live drawer and call history
both used to display these as separate calls, which surprised agents
("why is the same caller here twice?").

This migration adds ``master_session_id`` so we can group by
``COALESCE(master_session_id, session_id)`` — collapses transfer legs
to one row while leaving non-transfer calls untouched (their
masterContactId equals their contactId, so the COALESCE is a no-op).

Backfilled in a follow-up step (see ``cli.backfill_master_session_id``).

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-05-04 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite doesn't support adding indexed columns in one shot; do the
    # column add inside a batch_alter_table so the table is rebuilt
    # cleanly when needed.
    with op.batch_alter_table('call_event') as batch:
        batch.add_column(sa.Column('master_session_id', sa.String(length=120), nullable=True))
    op.create_index('ix_call_event_master_session_id', 'call_event', ['master_session_id'])


def downgrade():
    op.drop_index('ix_call_event_master_session_id', table_name='call_event')
    with op.batch_alter_table('call_event') as batch:
        batch.drop_column('master_session_id')
