"""Read the Neto Advanced-Configuration snapshot from BigQuery + assemble it.

Canonical config lives in BigQuery ``neto_config.*`` (written by the cPanel
scraper). This service reads the latest snapshot for the NETO Advanced Config
tab, parses each variable's enum options, groups by module, and computes a small
summary. Results are mirrored into SQLite for fast page loads; the tab reads the
mirror and only touches BigQuery on a miss or after a refresh.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

PROJECT = "chainsawspares-385722"
DATASET = "neto_config"
_TTL = 1800  # 30 min

log = logging.getLogger(__name__)
_cache: dict[str, tuple[float, Any]] = {}


def _bq():
    from app.services.purchase_orders_service import purchase_orders_service
    return purchase_orders_service.client


def _cached(key: str, loader):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = loader()
    _cache[key] = (now + _TTL, value)
    return value


def bust_cache() -> None:
    _cache.clear()


def _rows(sql: str) -> list[dict]:
    client = _bq()
    if client is None:
        raise RuntimeError("BigQuery client unavailable")
    return [dict(r) for r in client.query(sql).result()]


def _latest_snapshot_id() -> str | None:
    rows = _rows(
        f"SELECT MAX(snapshot_id) AS sid FROM `{PROJECT}.{DATASET}.scrape_runs` "
        "WHERE status = 'ok'"
    )
    return rows[0]["sid"] if rows and rows[0]["sid"] else None


def get_snapshot() -> dict[str, Any]:
    """Latest config snapshot: {snapshot_id, vars:[...], meta:{...}}."""
    def loader():
        sid = _latest_snapshot_id()
        if not sid:
            return {"snapshot_id": None, "vars": [], "meta": {}}
        rows = _rows(
            f"SELECT config_id, name, module, mod, title, value, type_raw, "
            f"is_system, is_readonly, is_custom, data_type, description, "
            f"options_json, detail_ok "
            f"FROM `{PROJECT}.{DATASET}.config_vars` WHERE snapshot_id = '{sid}' "
            f"ORDER BY module, name"
        )
        for r in rows:
            try:
                r["options"] = json.loads(r.pop("options_json") or "[]")
            except (TypeError, ValueError):
                r["options"] = []
        meta = _rows(
            f"SELECT snapshot_id, scraped_at, source, duration_s, status, "
            f"n_vars, n_detail_ok, n_modules "
            f"FROM `{PROJECT}.{DATASET}.scrape_runs` WHERE snapshot_id = '{sid}'"
        )
        return {"snapshot_id": sid, "vars": rows, "meta": meta[0] if meta else {}}

    return _cached("snapshot", loader)


def build_payload() -> dict:
    """Assemble the snapshot into the structure the tab renders: variables list,
    per-module counts, and a summary."""
    snap = get_snapshot()
    variables = snap["vars"]

    modules: dict[str, int] = {}
    for v in variables:
        modules[v.get("module") or "—"] = modules.get(v.get("module") or "—", 0) + 1
    module_counts = [{"module": m, "count": c}
                     for m, c in sorted(modules.items(), key=lambda kv: (-kv[1], kv[0]))]

    summary = {
        "total": len(variables),
        "modules": len(modules),
        "readonly": sum(1 for v in variables if v.get("is_readonly")),
        "editable": sum(1 for v in variables if not v.get("is_readonly")),
        "custom": sum(1 for v in variables if v.get("is_custom")),
        "with_description": sum(1 for v in variables if v.get("description")),
    }
    return {
        "snapshot_id": snap["snapshot_id"],
        "meta": snap["meta"],
        "vars": variables,
        "module_counts": module_counts,
        "summary": summary,
    }


def mirror_to_local(payload: dict | None = None) -> str | None:
    """Rebuild the SQLite mirror from BigQuery (or a supplied payload)."""
    from app.extensions import db
    from app.models.neto_config import NetoConfigMirror

    if payload is None:
        bust_cache()
        payload = build_payload()

    meta = payload.get("meta") or {}
    row = NetoConfigMirror(
        snapshot_id=payload.get("snapshot_id"),
        scraped_at=str(meta.get("scraped_at") or ""),
        source=str(meta.get("source") or ""),
        payload=json.dumps(payload, default=str),
    )
    NetoConfigMirror.query.delete()
    db.session.add(row)
    db.session.commit()
    return payload.get("snapshot_id")


def get_local() -> dict | None:
    """Return the assembled snapshot from the SQLite mirror, or None if empty."""
    from app.models.neto_config import NetoConfigMirror
    row = NetoConfigMirror.query.order_by(NetoConfigMirror.id.desc()).first()
    if not row:
        return None
    return json.loads(row.payload)
