"""add weather cache tables

Revision ID: b7d8e9f0a1c2
Revises: a6c7d8e9f0b1
Create Date: 2026-06-04

Local SQLite cache of the latest weather snapshot + 30-day alert history for
the Shop Order > Weather & Alerts tab. Mirrors operations.weather_current /
weather_forecast / weather_alerts. Rebuilt by
app.services.shop_order_cache.cache_weather_data on the :05/:35 refresh timer.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7d8e9f0a1c2'
down_revision = 'a6c7d8e9f0b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cached_weather_current',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('temp_c', sa.Float(), nullable=True),
        sa.Column('apparent_c', sa.Float(), nullable=True),
        sa.Column('precip_mm', sa.Float(), nullable=True),
        sa.Column('wind_kmh', sa.Float(), nullable=True),
        sa.Column('weather_label', sa.String(length=40), nullable=True),
        sa.Column('is_day', sa.Boolean(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_weather_current_fetched_at', 'cached_weather_current', ['fetched_at'])
    op.create_index('ix_cached_weather_current_cached_at', 'cached_weather_current', ['cached_at'])

    op.create_table(
        'cached_weather_forecast',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('forecast_date', sa.String(length=12), nullable=True),
        sa.Column('day_offset', sa.Integer(), nullable=True),
        sa.Column('temp_min', sa.Float(), nullable=True),
        sa.Column('temp_max', sa.Float(), nullable=True),
        sa.Column('precip_mm', sa.Float(), nullable=True),
        sa.Column('precip_prob_max', sa.Integer(), nullable=True),
        sa.Column('wind_max_kmh', sa.Float(), nullable=True),
        sa.Column('weather_label', sa.String(length=40), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_weather_forecast_day_offset', 'cached_weather_forecast', ['day_offset'])
    op.create_index('ix_cached_weather_forecast_cached_at', 'cached_weather_forecast', ['cached_at'])

    op.create_table(
        'cached_weather_alert',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.String(length=40), nullable=True),
        sa.Column('feed_type', sa.String(length=30), nullable=True),
        sa.Column('category1', sa.String(length=50), nullable=True),
        sa.Column('category2', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('headline', sa.String(length=300), nullable=True),
        sa.Column('action', sa.String(length=120), nullable=True),
        sa.Column('location', sa.String(length=300), nullable=True),
        sa.Column('alert_text', sa.Text(), nullable=True),
        sa.Column('created', sa.String(length=40), nullable=True),
        sa.Column('updated', sa.String(length=40), nullable=True),
        sa.Column('distance_km', sa.Float(), nullable=True),
        sa.Column('url', sa.String(length=300), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('cached_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cached_weather_alert_source_id', 'cached_weather_alert', ['source_id'])
    op.create_index('ix_cached_weather_alert_category1', 'cached_weather_alert', ['category1'])
    op.create_index('ix_cached_weather_alert_distance_km', 'cached_weather_alert', ['distance_km'])
    op.create_index('ix_cached_weather_alert_cached_at', 'cached_weather_alert', ['cached_at'])


def downgrade():
    op.drop_table('cached_weather_alert')
    op.drop_table('cached_weather_forecast')
    op.drop_table('cached_weather_current')
