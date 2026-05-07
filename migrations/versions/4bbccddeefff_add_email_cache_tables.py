"""add email cache tables

Revision ID: 4bbccddeefff
Revises: 3aabbccddeef
Create Date: 2026-05-07 14:45:00.000000

Local mirror of ``email_archive.messages`` for the Customer 360 Email
History panel. Two tables: ``cached_email_message`` for the message
itself, and ``cached_email_recipient`` flattened so panel lookups can
match any address (from / to / cc / bcc) with a single indexed scan.
"""
from alembic import op
import sqlalchemy as sa


revision = '4bbccddeefff'
down_revision = '3aabbccddeef'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cached_email_message',
        sa.Column('message_id', sa.String(length=255), nullable=False),
        sa.Column('conversation_id', sa.String(length=255), nullable=True),
        sa.Column('from_address', sa.String(length=255), nullable=True),
        sa.Column('from_name', sa.String(length=255), nullable=True),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.Column('direction', sa.String(length=10), nullable=True),
        sa.Column('is_automated', sa.Boolean(), nullable=True),
        sa.Column('has_attachments', sa.Boolean(), nullable=True),
        sa.Column('body_preview', sa.Text(), nullable=True),
        sa.Column('parent_folder_name', sa.String(length=255), nullable=True),
        sa.Column('web_link', sa.Text(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('message_id'),
    )
    with op.batch_alter_table('cached_email_message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cached_email_message_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cached_email_message_from_address'), ['from_address'], unique=False)
        batch_op.create_index(batch_op.f('ix_cached_email_message_received_at'), ['received_at'], unique=False)

    op.create_table('cached_email_recipient',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.String(length=255), nullable=False),
        sa.Column('address', sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('cached_email_recipient', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cached_email_recipient_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cached_email_recipient_address'), ['address'], unique=False)


def downgrade():
    with op.batch_alter_table('cached_email_recipient', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_email_recipient_address'))
        batch_op.drop_index(batch_op.f('ix_cached_email_recipient_message_id'))
    op.drop_table('cached_email_recipient')

    with op.batch_alter_table('cached_email_message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cached_email_message_received_at'))
        batch_op.drop_index(batch_op.f('ix_cached_email_message_from_address'))
        batch_op.drop_index(batch_op.f('ix_cached_email_message_conversation_id'))
    op.drop_table('cached_email_message')
