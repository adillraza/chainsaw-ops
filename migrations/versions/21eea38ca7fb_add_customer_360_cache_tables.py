"""add customer 360 cache tables

Revision ID: 21eea38ca7fb
Revises: f4a5b6c7d8e9
Create Date: 2026-05-07 13:11:29.400533

Five JSON-blob cache tables that mirror the Dataform models driving the
live-call Customer 360 card. Refreshed by ``flask refresh-cache`` on the
``chainsaw-ops-refresh.timer`` schedule.
"""
from alembic import op
import sqlalchemy as sa


revision = '21eea38ca7fb'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cached_call_behavior',
        sa.Column('phone', sa.String(length=50), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('phone'),
    )
    op.create_table('cached_call_history',
        sa.Column('phone', sa.String(length=50), nullable=False),
        sa.Column('last_call_date', sa.Date(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('phone'),
    )
    op.create_table('cached_customer_360',
        sa.Column('Username', sa.String(length=150), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('secondary_email', sa.String(length=255), nullable=True),
        sa.Column('last_order_date', sa.Date(), nullable=True),
        sa.Column('last_rma_date', sa.Date(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('Username'),
    )
    with op.batch_alter_table('cached_customer_360', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cached_customer_360_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_cached_customer_360_secondary_email'), ['secondary_email'], unique=False)

    op.create_table('cached_neto_product',
        sa.Column('sku', sa.String(length=100), nullable=False),
        sa.Column('product_id', sa.String(length=50), nullable=True),
        sa.Column('name', sa.String(length=500), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('sku'),
    )
    op.create_table('cached_phone_lookup',
        sa.Column('phone', sa.String(length=50), nullable=False),
        sa.Column('usernames_json', sa.Text(), nullable=False),
        sa.Column('match_count', sa.Integer(), nullable=True),
        sa.Column('is_international', sa.Boolean(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('phone'),
    )


def downgrade():
    op.drop_table('cached_phone_lookup')
    op.drop_table('cached_neto_product')
    with op.batch_alter_table('cached_customer_360', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_customer_360_secondary_email'))
        batch_op.drop_index(batch_op.f('ix_cached_customer_360_email'))
    op.drop_table('cached_customer_360')
    op.drop_table('cached_call_history')
    op.drop_table('cached_call_behavior')
