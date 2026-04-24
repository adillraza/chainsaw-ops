"""add is_kitted_item to cached PO tables

Adds the ``is_kitted_item`` boolean column (with index) to both the cached
items and cached comparison tables. Kits shouldn't appear on a PO, so the
flag is rendered as a warning badge in the UI.

Revision ID: 9d3a8b4c5e6f
Revises: 8c9e2f3a4d5b
Create Date: 2026-04-20 17:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9d3a8b4c5e6f'
down_revision = '8c9e2f3a4d5b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cached_purchase_order_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_kitted_item', sa.Boolean(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_cached_purchase_order_item_is_kitted_item'),
            ['is_kitted_item'],
            unique=False,
        )
    with op.batch_alter_table('cached_purchase_order_comparison', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_kitted_item', sa.Boolean(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_cached_purchase_order_comparison_is_kitted_item'),
            ['is_kitted_item'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('cached_purchase_order_comparison', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_purchase_order_comparison_is_kitted_item'))
        batch_op.drop_column('is_kitted_item')
    with op.batch_alter_table('cached_purchase_order_item', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_purchase_order_item_is_kitted_item'))
        batch_op.drop_column('is_kitted_item')
