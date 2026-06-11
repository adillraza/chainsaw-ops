"""add cached_related_accounts table

Revision ID: c8d9e0f1a2b3
Revises: b7d8e9f0a1c2
Create Date: 2026-06-12

Mirror of the Dataform ``customer_related_accounts`` model — per-Username
list of OTHER Neto usernames sharing an identity signal (same primary
email, same secondary email, primary↔secondary cross-match, or same
billing street + postcode). Drives the Customer 360 related-accounts
panels. Refreshed by ``flask refresh-cache`` alongside the other
customer-cache tables.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8d9e0f1a2b3'
down_revision = 'b7d8e9f0a1c2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cached_related_accounts',
        sa.Column('Username', sa.String(length=150), nullable=False),
        sa.Column('related_json', sa.Text(), nullable=False),
        sa.Column('related_count', sa.Integer(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('Username'),
    )


def downgrade():
    op.drop_table('cached_related_accounts')
