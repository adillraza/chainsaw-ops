"""add user.prefers_v2

Revision ID: 2cb808a9246f
Revises: 4caa217c82df
Create Date: 2026-04-20 15:58:53.726821

Backs the per-user feature flag that opts into the new Tailwind shell.
"""
from alembic import op
import sqlalchemy as sa


revision = '2cb808a9246f'
down_revision = '4caa217c82df'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'prefers_v2',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('prefers_v2')
