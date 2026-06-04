"""add seasonality index cache table

Revision ID: a6c7d8e9f0b1
Revises: f5b6c7d8e9a0
Create Date: 2026-06-04

Local SQLite cache of dataform.rex_seasonality_index (product_type x month
seasonal curves) for the Shop Order > Seasonality Index tab. Rebuilt by
app.services.shop_order_cache on the :05/:35 refresh timer.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a6c7d8e9f0b1'
down_revision = 'f5b6c7d8e9a0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cached_seasonality_index',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_type', sa.String(length=150), nullable=True),
        sa.Column('month', sa.Integer(), nullable=True),
        sa.Column('seasonal_index', sa.Float(), nullable=True),
        sa.Column('sample_units', sa.Integer(), nullable=True),
        sa.Column('years_covered', sa.Integer(), nullable=True),
        sa.Column('confidence', sa.String(length=12), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_seasonality_index_product_type', 'cached_seasonality_index', ['product_type'])
    op.create_index('ix_cached_seasonality_index_month', 'cached_seasonality_index', ['month'])
    op.create_index('ix_cached_seasonality_index_cached_at', 'cached_seasonality_index', ['cached_at'])


def downgrade():
    op.drop_table('cached_seasonality_index')
