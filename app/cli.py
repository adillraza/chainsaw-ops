"""Flask CLI commands. Registered from :func:`app.create_app`.

Invoked on the server by the ``chainsaw-ops-refresh.timer`` systemd unit
(see ``deploy/systemd/``) to keep the local cache in sync with BigQuery
shortly after the Dataform workflow finishes its half-hourly run.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from flask import Flask
from flask.cli import with_appcontext

REFRESH_LOCK = Path("/tmp/chainsaw-ops-refresh.lock")


def register(app: Flask) -> None:
    app.cli.add_command(refresh_cache)
    app.cli.add_command(reparse_call_events)
    app.cli.add_command(fetch_weather)


def _acquire_lock() -> bool:
    """Best-effort lockfile so two refreshes don't run concurrently.

    The first ever refresh is a ~15-minute full load; the systemd timer
    fires every 30 min, so without a guard a second invocation could
    start before the first finishes and corrupt the half-built cache.
    Returns True if we got the lock, False if another run is in
    progress.
    """
    try:
        # Stale lock cleanup: if the lockfile is older than 60 minutes
        # the previous run is almost certainly dead.
        if REFRESH_LOCK.exists():
            age = time.time() - REFRESH_LOCK.stat().st_mtime
            if age < 60 * 60:
                return False
            REFRESH_LOCK.unlink(missing_ok=True)
        REFRESH_LOCK.write_text(f"pid={os.getpid()} t={int(time.time())}")
        return True
    except Exception:
        return True  # don't block on lock-fs errors


def _release_lock() -> None:
    try:
        REFRESH_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


@click.command("refresh-cache")
@with_appcontext
def refresh_cache() -> None:
    """Re-fetch the PO and Customer-360 caches from BigQuery into SQLite.

    Both caches are refreshed in sequence — PO first (the older cache the UI
    Refresh Data button still uses), then customer_360 + phone_lookup +
    call_history + call_behavior + neto_product (all driving the live-call
    Customer 360 card).

    A failure on either side is logged but does not block the other; we'd
    rather have one stale cache than zero.
    """
    if not _acquire_lock():
        click.echo("refresh-cache: another run is in progress (lockfile present); skipping",
                   err=True)
        return

    try:
        from app.services.cache import cache_purchase_order_data
        from app.services.customer_cache import cache_customer_360_data
        from app.services.email_cache import cache_email_archive
        from app.services.shop_order_cache import (
            cache_shop_order_data,
            cache_weather_data,
        )

        failures: list[str] = []

        success, message = cache_purchase_order_data()
        click.echo(f"refresh-cache (PO): {message}", err=not success)
        if not success:
            failures.append("PO")

        success, message = cache_shop_order_data()
        click.echo(f"refresh-cache (shop_order): {message}", err=not success)
        if not success:
            failures.append("shop_order")

        success, message = cache_weather_data()
        click.echo(f"refresh-cache (weather): {message}", err=not success)
        if not success:
            failures.append("weather")

        success, message = cache_customer_360_data()
        click.echo(f"refresh-cache (customer_360): {message}", err=not success)
        if not success:
            failures.append("customer_360")

        success, message = cache_email_archive()
        click.echo(f"refresh-cache (email_archive): {message}", err=not success)
        if not success:
            failures.append("email_archive")

        if failures:
            raise SystemExit(1)
    finally:
        _release_lock()


@click.command("fetch-weather")
@with_appcontext
def fetch_weather() -> None:
    """Fetch Ballarat weather + Victorian alerts into BigQuery (4-hourly timer)."""
    from app.services.weather_service import ingest_weather_and_alerts
    success, message = ingest_weather_and_alerts()
    click.echo(f"fetch-weather: {message}", err=not success)
    if not success:
        raise SystemExit(1)


@click.command("reparse-call-events")
@with_appcontext
def reparse_call_events() -> None:
    """Re-run the live_calls parser over every stored call_event row.

    Used after deploying a parser change so old rows pick up the new
    event_type / session_id / from_number / to_number values.
    """
    from app.blueprints.live_calls.routes import reparse_all_call_events
    updated, total = reparse_all_call_events()
    click.echo(f"reparse-call-events: updated {updated} of {total} rows")
