"""add microsoft sso columns to user

Revision ID: 5cdeeff01122
Revises: 4bbccddeefff
Create Date: 2026-05-08 09:00:00.000000

Adds the columns Microsoft Entra SSO needs:
* ``microsoft_oid`` — the stable Entra object ID (unique). Source of
  truth for matching a returning sign-in to a local user row.
* ``microsoft_upn`` — the user-principal-name (e.g. ``fabio@…``).
  Display + admin lookup; can change over time.
* ``display_name`` — pulled from the ID-token ``name`` claim on first
  sign-in, used in the topbar / activity logs.
* ``last_microsoft_login_at`` — for audit + dormant-account spotting.

Also relaxes ``password_hash`` to nullable so SSO-only users (no local
password, no M365 license) are valid User rows.
"""
from alembic import op
import sqlalchemy as sa


revision = '5cdeeff01122'
down_revision = '4bbccddeefff'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('microsoft_oid', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('microsoft_upn', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('display_name',  sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('last_microsoft_login_at', sa.DateTime(), nullable=True))
        batch_op.alter_column('password_hash',
                              existing_type=sa.String(length=120),
                              type_=sa.String(length=255),
                              nullable=True)
        batch_op.create_index(batch_op.f('ix_user_microsoft_oid'),
                              ['microsoft_oid'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_microsoft_upn'),
                              ['microsoft_upn'], unique=False)


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_microsoft_upn'))
        batch_op.drop_index(batch_op.f('ix_user_microsoft_oid'))
        batch_op.alter_column('password_hash',
                              existing_type=sa.String(length=255),
                              type_=sa.String(length=120),
                              nullable=False)
        batch_op.drop_column('last_microsoft_login_at')
        batch_op.drop_column('display_name')
        batch_op.drop_column('microsoft_upn')
        batch_op.drop_column('microsoft_oid')
