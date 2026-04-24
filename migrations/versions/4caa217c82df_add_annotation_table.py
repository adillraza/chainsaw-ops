"""add annotation table

Revision ID: 4caa217c82df
Revises: 0aa9409a2368
Create Date: 2026-04-20 15:57:22.806697

Only the annotation table + its indices are introduced here. Pre-existing
schema drift between the legacy app.py model definitions and the live SQLite
DB is left intentionally untouched (Phase 1 = no behaviour change).
"""
from alembic import op
import sqlalchemy as sa


revision = '4caa217c82df'
down_revision = '0aa9409a2368'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'annotation',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('entity_type', sa.String(length=50), nullable=False),
        sa.Column('entity_id', sa.String(length=100), nullable=False),
        sa.Column('parent_id', sa.String(length=32), nullable=True),
        sa.Column('comment', sa.Text(), nullable=False),
        sa.Column('author_id', sa.Integer(), nullable=False),
        sa.Column('author_username', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['user.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['annotation.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('annotation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_annotation_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_annotation_deleted_at'), ['deleted_at'], unique=False)
        batch_op.create_index('ix_annotation_entity', ['entity_type', 'entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_annotation_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_annotation_entity_type'), ['entity_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_annotation_parent_id'), ['parent_id'], unique=False)


def downgrade():
    with op.batch_alter_table('annotation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_annotation_parent_id'))
        batch_op.drop_index(batch_op.f('ix_annotation_entity_type'))
        batch_op.drop_index(batch_op.f('ix_annotation_entity_id'))
        batch_op.drop_index('ix_annotation_entity')
        batch_op.drop_index(batch_op.f('ix_annotation_deleted_at'))
        batch_op.drop_index(batch_op.f('ix_annotation_created_at'))
    op.drop_table('annotation')
