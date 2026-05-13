"""Add call_sensitivity_flag table for gating management-portion calls.

Revision ID: 6def01122334
Revises: 5cdeeff01122
Create Date: 2026-05-14 09:00:00.000000

A row in this table means the call (keyed by ``session_id``) contains
sensitive content — typically a management portion (escalation, HR
discussion, supervisor coaching). When the call-details modal renders,
the customer 360 service joins this table; if a row exists AND the
viewing user lacks ``support.calls.view_sensitive``, the service
strips summary / transcription / audio URL / sentiment / classifications
from the payload server-side, and the modal shows only the basic
metadata plus a "restricted" banner.

Unflagging deletes the row, so audit attribution lives only as long as
the flag does. If richer audit is needed later, a separate
``call_sensitivity_audit_log`` table can be added without changing this
one.
"""
from alembic import op
import sqlalchemy as sa


revision = '6def01122334'
down_revision = '5cdeeff01122'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite can't ALTER TABLE ADD CONSTRAINT, so the UNIQUE constraint
    # is inlined into CREATE TABLE rather than added afterwards (same
    # pattern as pinned_call).
    op.create_table(
        'call_sensitivity_flag',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.String(length=120), nullable=False, unique=True),
        sa.Column('flagged_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        # Display + audit attribution. Nullable so the column survives
        # the source User row being deleted (FK is ON DELETE SET NULL
        # in effect via nullable=True; the flag itself isn't tied to
        # the flagger's continued existence).
        sa.Column('flagged_by_user_id', sa.Integer(),
                  sa.ForeignKey('user.id'), nullable=True),
        sa.Column('reason', sa.String(length=240), nullable=True),
    )
    op.create_index('ix_call_sensitivity_flag_session_id',
                    'call_sensitivity_flag', ['session_id'])
    op.create_index('ix_call_sensitivity_flag_flagged_by_user_id',
                    'call_sensitivity_flag', ['flagged_by_user_id'])


def downgrade():
    op.drop_index('ix_call_sensitivity_flag_flagged_by_user_id',
                  table_name='call_sensitivity_flag')
    op.drop_index('ix_call_sensitivity_flag_session_id',
                  table_name='call_sensitivity_flag')
    op.drop_table('call_sensitivity_flag')
