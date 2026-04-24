"""add neto_product_id to cached_purchase_order_comparison

Mirrors the column added to ``cached_purchase_order_item`` so the comparison
view can also deep-link the SKU to the Neto cpanel product page.

Revision ID: 8c9e2f3a4d5b
Revises: 7b21de4d9c11
Create Date: 2026-04-20 16:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '8c9e2f3a4d5b'
down_revision = '7b21de4d9c11'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cached_purchase_order_comparison', schema=None) as batch_op:
        batch_op.add_column(sa.Column('neto_product_id', sa.String(length=50), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_cached_purchase_order_comparison_neto_product_id'),
            ['neto_product_id'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('cached_purchase_order_comparison', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_purchase_order_comparison_neto_product_id'))
        batch_op.drop_column('neto_product_id')
