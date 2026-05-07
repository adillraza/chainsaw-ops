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
    app.cli.add_command(reparse_call_events)


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
    from app.services.cache import cache_purchase_order_data
    from app.services.customer_cache import cache_customer_360_data

    failures: list[str] = []

    success, message = cache_purchase_order_data()
    click.echo(f"refresh-cache (PO): {message}",
               err=not success)
    if not success:
        failures.append("PO")

    success, message = cache_customer_360_data()
    click.echo(f"refresh-cache (customer_360): {message}",
               err=not success)
    if not success:
        failures.append("customer_360")

    if failures:
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
