"""Flask CLI commands. Registered from :func:`app.create_app`.

Invoked on the server by the ``chainsaw-ops-refresh.timer`` systemd unit
(see ``deploy/systemd/``) to keep the local cache in sync with BigQuery
shortly after the Dataform workflow finishes its half-hourly run.
"""
from __future__ import annotations

import click
from flask import Flask
from flask.cli import with_appcontext


def register(app: Flask) -> None:
    app.cli.add_command(refresh_cache)


@click.command("refresh-cache")
@with_appcontext
def refresh_cache() -> None:
    """Re-fetch all PO data from BigQuery into the local SQLite cache.

    No-ops cleanly if a sync is already in progress (same guard the UI
    Refresh Data button uses).
    """
    from app.services.cache import cache_purchase_order_data

    success, message = cache_purchase_order_data()
    if not success:
        click.echo(f"refresh-cache: {message}", err=True)
        raise SystemExit(1)
    click.echo(f"refresh-cache: {message}")
