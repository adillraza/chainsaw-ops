"""Write a parsed Neto Advanced-Configuration snapshot to BigQuery.

Canonical store: dataset ``neto_config`` in ``chainsawspares-385722``.
Append-snapshot model — every scrape tags rows with a ``snapshot_id`` (the
scrape timestamp); the latest snapshot is ``MAX(snapshot_id)`` where status='ok'.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from google.cloud import bigquery

PROJECT = "chainsawspares-385722"
DATASET = "neto_config"

SCHEMAS = {
    "config_vars": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("config_id", "STRING"), ("name", "STRING"), ("module", "STRING"),
        ("mod", "STRING"), ("title", "STRING"), ("value", "STRING"),
        ("type_raw", "STRING"), ("is_system", "BOOL"), ("is_readonly", "BOOL"),
        ("is_custom", "BOOL"), ("data_type", "STRING"), ("description", "STRING"),
        ("options_json", "STRING"), ("detail_ok", "BOOL"),
    ],
    "scrape_runs": [
        ("snapshot_id", "STRING"), ("scraped_at", "TIMESTAMP"),
        ("duration_s", "FLOAT"), ("status", "STRING"), ("source", "STRING"),
        ("n_vars", "INTEGER"), ("n_detail_ok", "INTEGER"),
        ("n_modules", "INTEGER"), ("error", "STRING"),
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
        client.create_table(bigquery.Table(f"{PROJECT}.{DATASET}.{table}", schema=schema),
                            exists_ok=True)
    return client


def _coerce(rows, cols):
    names = [c for c, _ in cols]
    return [{n: r.get(n) for n in names} for r in rows]


def new_snapshot_id():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.isoformat()


def write_snapshot(snapshot, source="ui-refresh", client=None):
    """``snapshot`` = {snapshot_id, scraped_at, vars:[...], duration_s, status, error}.

    Each var dict may carry an ``options`` list — serialised to ``options_json``.
    """
    client = ensure_dataset_and_tables(client)
    sid = snapshot["snapshot_id"]
    scraped_at = snapshot["scraped_at"]

    vars_rows = []
    modules = set()
    n_detail_ok = 0
    for v in snapshot.get("vars", []):
        modules.add(v.get("module"))
        if v.get("detail_ok"):
            n_detail_ok += 1
        row = dict(v)
        row["snapshot_id"] = sid
        row["scraped_at"] = scraped_at
        row["options_json"] = json.dumps(v.get("options") or [])
        vars_rows.append(row)

    runs_rows = [{
        "snapshot_id": sid, "scraped_at": scraped_at,
        "duration_s": snapshot.get("duration_s"),
        "status": snapshot.get("status", "ok"), "source": source,
        "n_vars": len(vars_rows), "n_detail_ok": n_detail_ok,
        "n_modules": len([m for m in modules if m]),
        "error": snapshot.get("error"),
    }]

    payload = {"config_vars": vars_rows, "scrape_runs": runs_rows}
    job_cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    for table, cols in SCHEMAS.items():
        rows = _coerce(payload[table], cols)
        if not rows:
            continue
        job_cfg.schema = [bigquery.SchemaField(c, t) for c, t in cols]
        client.load_table_from_json(
            rows, f"{PROJECT}.{DATASET}.{table}", job_config=job_cfg
        ).result()
    return sid
