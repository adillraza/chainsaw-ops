"""add shop order cache tables

Revision ID: f5b6c7d8e9a0
Revises: b8d2e1c3f4a5
Create Date: 2026-06-04

Local SQLite cache of the two Shop-Order recommendation models so the Ops
"Shop Order" screen loads instantly:
  cached_shop_order_msl   <- dataform.po_preview_lines
  cached_shop_order_smart <- dataform.rex_po_recommendation
Rebuilt by app.services.shop_order_cache on the :05/:35 refresh timer.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f5b6c7d8e9a0'
down_revision = 'b8d2e1c3f4a5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cached_shop_order_msl',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bucket', sa.Integer(), nullable=True),
        sa.Column('line_type', sa.String(length=20), nullable=True),
        sa.Column('manufacturer_sku', sa.String(length=100), nullable=True),
        sa.Column('short_description', sa.String(length=500), nullable=True),
        sa.Column('product_type_name', sa.String(length=150), nullable=True),
        sa.Column('msl', sa.Integer(), nullable=True),
        sa.Column('available', sa.Integer(), nullable=True),
        sa.Column('on_order', sa.Integer(), nullable=True),
        sa.Column('re_order_qty', sa.Integer(), nullable=True),
        sa.Column('sold_last_14_days', sa.Integer(), nullable=True),
        sa.Column('sold_next_14_days_last_year', sa.Integer(), nullable=True),
        sa.Column('seasonal_bump', sa.Integer(), nullable=True),
        sa.Column('raw_qty', sa.Integer(), nullable=True),
        sa.Column('carton_quantity', sa.Integer(), nullable=True),
        sa.Column('proposed_qty', sa.Integer(), nullable=True),
        sa.Column('adjustment_qty', sa.Integer(), nullable=True),
        sa.Column('supplier_buy_ex', sa.Float(), nullable=True),
        sa.Column('estimated_line_value', sa.Float(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_shop_order_msl_bucket', 'cached_shop_order_msl', ['bucket'])
    op.create_index('ix_cached_shop_order_msl_line_type', 'cached_shop_order_msl', ['line_type'])
    op.create_index('ix_cached_shop_order_msl_manufacturer_sku', 'cached_shop_order_msl', ['manufacturer_sku'])
    op.create_index('ix_cached_shop_order_msl_cached_at', 'cached_shop_order_msl', ['cached_at'])

    op.create_table(
        'cached_shop_order_smart',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bucket', sa.Integer(), nullable=True),
        sa.Column('urgency', sa.String(length=20), nullable=True),
        sa.Column('category', sa.String(length=20), nullable=True),
        sa.Column('manufacturer_sku', sa.String(length=100), nullable=True),
        sa.Column('short_description', sa.String(length=500), nullable=True),
        sa.Column('product_type_name', sa.String(length=150), nullable=True),
        sa.Column('available', sa.Integer(), nullable=True),
        sa.Column('msl', sa.Integer(), nullable=True),
        sa.Column('on_order', sa.Integer(), nullable=True),
        sa.Column('carton_quantity', sa.Integer(), nullable=True),
        sa.Column('s14', sa.Integer(), nullable=True),
        sa.Column('s30', sa.Integer(), nullable=True),
        sa.Column('lyr30', sa.Integer(), nullable=True),
        sa.Column('yr2_30', sa.Integer(), nullable=True),
        sa.Column('daily_velocity', sa.Float(), nullable=True),
        sa.Column('seasonal_factor', sa.Float(), nullable=True),
        sa.Column('forecast_30d', sa.Integer(), nullable=True),
        sa.Column('coverage_days', sa.Integer(), nullable=True),
        sa.Column('lead_days', sa.Integer(), nullable=True),
        sa.Column('recommended_qty', sa.Integer(), nullable=True),
        sa.Column('supplier_buy_ex', sa.Float(), nullable=True),
        sa.Column('estimated_line_value', sa.Float(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_shop_order_smart_bucket', 'cached_shop_order_smart', ['bucket'])
    op.create_index('ix_cached_shop_order_smart_urgency', 'cached_shop_order_smart', ['urgency'])
    op.create_index('ix_cached_shop_order_smart_manufacturer_sku', 'cached_shop_order_smart', ['manufacturer_sku'])
    op.create_index('ix_cached_shop_order_smart_cached_at', 'cached_shop_order_smart', ['cached_at'])


def downgrade():
    op.drop_table('cached_shop_order_smart')
    op.drop_table('cached_shop_order_msl')
