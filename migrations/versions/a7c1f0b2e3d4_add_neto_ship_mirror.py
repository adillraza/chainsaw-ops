"""add neto_ship_mirror table

Revision ID: a7c1f0b2e3d4
Revises: 6def01122334
Create Date: 2026-06-02

Local SQLite mirror of the BigQuery neto_shipping snapshot so the NETO
Shippings tab loads from local instead of querying BigQuery each page load.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a7c1f0b2e3d4'
down_revision = '6def01122334'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'neto_ship_mirror',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_id', sa.String(length=40), nullable=True),
        sa.Column('scraped_at', sa.String(length=40), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=True),
        sa.Column('mirrored_at', sa.DateTime(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('neto_ship_mirror')
