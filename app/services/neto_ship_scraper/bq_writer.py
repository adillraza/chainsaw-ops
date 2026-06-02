"""Write a parsed Neto shipping-config snapshot to BigQuery.

Canonical store: dataset ``neto_shipping`` in ``chainsawspares-385722``.
Append-snapshot model — every scrape tags its rows with a ``snapshot_id``
(the scrape timestamp) so history is retained and the latest snapshot is
``MAX(snapshot_id)``. The ops dashboard reads the latest by default.
"""
from __future__ import annotations

from datetime import datetime, timezone

from google.cloud import bigquery

PROJECT = "chainsawspares-385722"
DATASET = "neto_shipping"

# table_name -> ordered (column, BQ type)
SCHEMAS = {
    "carriers": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("carrier_id", "STRING"), ("name", "STRING"),
        ("location", "STRING"), ("courier_zone", "STRING"),
        ("is_active", "BOOL"),
    ],
    "categories": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("category_id", "STRING"), ("name", "STRING"),
        ("description", "STRING"), ("is_default", "BOOL"),
        ("is_active", "BOOL"),
    ],
    "options": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("option_id", "STRING"), ("name", "STRING"), ("routing_group", "STRING"),
        ("description", "STRING"), ("max_charge", "FLOAT"),
        ("min_weight_kg", "FLOAT"), ("max_weight_kg", "FLOAT"),
        ("pickup", "STRING"), ("delivery_days", "STRING"),
        ("cutoff_time", "STRING"), ("availability", "STRING"),
        ("status", "STRING"), ("visibility", "STRING"), ("is_active", "BOOL"),
    ],
    "services": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("service_id", "STRING"), ("name", "STRING"), ("type", "STRING"),
        ("description", "STRING"), ("po_box", "BOOL"),
        ("status", "STRING"), ("is_active", "BOOL"),
        # config (from detail page; null until detail scrape runs)
        ("charge_type", "STRING"), ("cubic_modifier", "FLOAT"),
        ("tax_inclusive", "BOOL"), ("max_length_m", "FLOAT"),
        ("min_charge", "FLOAT"), ("max_charge", "FLOAT"),
        ("fuel_amt", "FLOAT"), ("fuel_pct", "FLOAT"),
        ("handling_amt", "FLOAT"), ("handling_unit", "STRING"),
    ],
    "mapping": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("block_index", "INTEGER"), ("routing_group", "STRING"),
        ("block_active", "BOOL"), ("block_visible", "BOOL"),
        ("category", "STRING"), ("service", "STRING"), ("carrier", "STRING"),
    ],
    "scrape_runs": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("duration_s", "FLOAT"), ("status", "STRING"), ("source", "STRING"),
        ("n_carriers", "INTEGER"), ("n_categories", "INTEGER"),
        ("n_options", "INTEGER"), ("n_services", "INTEGER"),
        ("n_mapping", "INTEGER"), ("error", "STRING"),
    ],
}


def _client():
    return bigquery.Client(project=PROJECT)


def ensure_dataset_and_tables(client=None):
    client = client or _client()
    ds_ref = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    ds_ref.location = "US"
    client.create_dataset(ds_ref, exists_ok=True)
    for table, cols in SCHEMAS.items():
        schema = [bigquery.SchemaField(c, t) for c, t in cols]
        table_ref = bigquery.Table(f"{PROJECT}.{DATASET}.{table}", schema=schema)
        client.create_table(table_ref, exists_ok=True)
    return client


def _coerce(rows, cols):
    """Keep only known columns, fill missing with None, in schema order."""
    names = [c for c, _ in cols]
    out = []
    for r in rows:
        out.append({n: r.get(n) for n in names})
    return out


def write_snapshot(snapshot, source="live", client=None):
    """``snapshot`` = dict with keys carriers/categories/options/services/mapping
    (lists of dicts) plus snapshot_id, scraped_at, duration_s, status, error."""
    client = ensure_dataset_and_tables(client)
    sid = snapshot["snapshot_id"]
    scraped_at = snapshot["scraped_at"]

    def stamp(rows):
        for r in rows:
            r.setdefault("snapshot_id", sid)
            r.setdefault("scraped_at", scraped_at)
        return rows

    payload = {
        "carriers": stamp(snapshot.get("carriers", [])),
        "categories": stamp(snapshot.get("categories", [])),
        "options": stamp(snapshot.get("options", [])),
        "services": stamp(snapshot.get("services", [])),
        "mapping": stamp(snapshot.get("mapping", [])),
        "scrape_runs": [{
            "snapshot_id": sid, "scraped_at": scraped_at,
            "duration_s": snapshot.get("duration_s"),
            "status": snapshot.get("status", "ok"),
            "source": source,
            "n_carriers": len(snapshot.get("carriers", [])),
            "n_categories": len(snapshot.get("categories", [])),
            "n_options": len(snapshot.get("options", [])),
            "n_services": len(snapshot.get("services", [])),
            "n_mapping": len(snapshot.get("mapping", [])),
            "error": snapshot.get("error"),
        }],
    }

    job_cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    for table, cols in SCHEMAS.items():
        rows = _coerce(payload[table], cols)
        if not rows:
            continue
        job_cfg.schema = [bigquery.SchemaField(c, t) for c, t in cols]
        job = client.load_table_from_json(
            rows, f"{PROJECT}.{DATASET}.{table}", job_config=job_cfg
        )
        job.result()
    return sid


def new_snapshot_id():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.isoformat()
