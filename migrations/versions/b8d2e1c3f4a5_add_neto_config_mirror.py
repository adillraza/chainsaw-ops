"""add neto_config_mirror table

Revision ID: b8d2e1c3f4a5
Revises: a7c1f0b2e3d4
Create Date: 2026-06-03

Local SQLite mirror of the BigQuery neto_config snapshot so the NETO Advanced
Config tab loads from local instead of querying BigQuery each page load.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8d2e1c3f4a5'
down_revision = 'a7c1f0b2e3d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'neto_config_mirror',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_id', sa.String(length=40), nullable=True),
        sa.Column('scraped_at', sa.String(length=40), nullable=True),
        sa.Column('source', sa.String(length=40), nullable=True),
        sa.Column('mirrored_at', sa.DateTime(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('neto_config_mirror')
