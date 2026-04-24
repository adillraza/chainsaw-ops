"""default prefers_v2 to true and backfill

Revision ID: e26ffedbbb04
Revises: 2cb808a9246f
Create Date: 2026-04-20 16:16:52.480712

This migration is the Phase 5 cutover step: the v2 shell is now the default
for all users. Existing accounts that still had ``prefers_v2 = 0`` are
flipped to ``1``. The column's server-side default is updated to match.

Drift detected by autogenerate (unrelated string-length / index changes on
``cached_purchase_order_summary`` and ``login_log``) is intentionally NOT
applied here — those models still match production and including the
detected diff would silently rebuild tables on SQLite. They will be handled
in their own migration if/when they actually need to change.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e26ffedbbb04'
down_revision = '2cb808a9246f'
branch_labels = None
depends_on = None


def upgrade():
    # Backfill any existing user rows that opted out of v2.
    op.execute("UPDATE user SET prefers_v2 = 1 WHERE prefers_v2 = 0 OR prefers_v2 IS NULL")

    # Update the server-side default so freshly-created users land on v2.
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column(
            'prefers_v2',
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.alter_column(
            'prefers_v2',
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )
