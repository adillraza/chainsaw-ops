"""add neto_product_id to cached_purchase_order_item

Adds the Neto product ID column to the cached items table so the cost-price
view can render hyperlinks straight to the Neto cpanel product page without
re-querying BigQuery on each request.

Revision ID: 7b21de4d9c11
Revises: e26ffedbbb04
Create Date: 2026-04-20 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '7b21de4d9c11'
down_revision = 'e26ffedbbb04'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cached_purchase_order_item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('neto_product_id', sa.String(length=50), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_cached_purchase_order_item_neto_product_id'),
            ['neto_product_id'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('cached_purchase_order_item', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_purchase_order_item_neto_product_id'))
        batch_op.drop_column('neto_product_id')
