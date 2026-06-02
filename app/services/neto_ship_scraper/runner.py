"""Run a full Neto shipping-config scrape and write a snapshot to BigQuery.

Invoked by the NETO Shippings "Update" button (background thread). Logs in
once, fetches the four list pages + the routing matrix + each active service
detail, parses, derives active flags, and appends a new BQ snapshot.

cPanel creds: env vars NETO_CPANEL_USERNAME / NETO_CPANEL_PASSWORD, with a
Secret Manager fallback (neto-cpanel-username / neto-cpanel-password).
"""
from __future__ import annotations

import logging
import os
import time

from . import parse as P
from . import bq_writer
from .session import create_session, BASE_URL

log = logging.getLogger(__name__)

LIST_PAGES = {
    "ship_carrier": f"{BASE_URL}/ship_carrier",
    "shippingid": f"{BASE_URL}/shippingid",
    "shippinggroup": f"{BASE_URL}/shippinggroup",
    "shippingcostmgr": f"{BASE_URL}/shippingcostmgr",
    "ship": f"{BASE_URL}/ship",
}


def _creds() -> tuple[str, str]:
    u, p = os.environ.get("NETO_CPANEL_USERNAME"), os.environ.get("NETO_CPANEL_PASSWORD")
    if u and p:
        return u, p
    # Secret Manager fallback (uses the same service account as BigQuery)
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        proj = bq_writer.PROJECT

        def sm(name):
            n = f"projects/{proj}/secrets/{name}/versions/latest"
            return client.access_secret_version(name=n).payload.data.decode().strip()
        return sm("neto-cpanel-username"), sm("neto-cpanel-password")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "No cPanel credentials — set NETO_CPANEL_USERNAME/PASSWORD env vars "
            f"(Secret Manager fallback failed: {exc})"
        )


def _derive_active(snap):
    active = [m for m in snap["mapping"] if m.get("block_active")]
    active_cats = {m["category"].strip().lower() for m in active if m.get("category")}
    blob = " ".join((m.get("carrier") or "") for m in active).lower()
    for c in snap["categories"]:
        c["is_active"] = c["name"].strip().lower() in active_cats
    for car in snap["carriers"]:
        nm, zn = (car.get("name") or "").lower(), (car.get("courier_zone") or "").lower()
        car["is_active"] = bool((nm and nm in blob) or (zn and zn in blob))
    return snap


def run_scrape(progress=None) -> dict:
    """Full scrape -> BigQuery snapshot. Returns the scrape_runs summary dict."""
    def log_progress(msg):
        log.info("[ship-scrape] %s", msg)
        if progress:
            progress(msg)

    t0 = time.time()
    user, pwd = _creds()
    log_progress("logging in to cPanel…")
    s = create_session(user, pwd)

    html = {}
    for key, url in LIST_PAGES.items():
        log_progress(f"fetching {key}…")
        html[key] = s.get(url).text
        time.sleep(0.3)

    snap = {
        "carriers": P.parse_carriers(html["ship_carrier"]),
        "categories": P.parse_categories(html["shippingid"]),
        "options": P.parse_options(html["shippinggroup"]),
        "services": P.parse_services(html["shippingcostmgr"]),
        "mapping": P.parse_mapping(html["ship"]),
    }

    active_services = [sv for sv in snap["services"] if sv["is_active"]]
    log_progress(f"fetching {len(active_services)} active service configs…")
    for i, sv in enumerate(active_services, 1):
        try:
            cfg = P.parse_service_detail(s.get(sv["detail_url"]).text)
            for k in ("charge_type", "cubic_modifier", "tax_inclusive", "max_length_m",
                      "min_charge", "max_charge", "fuel_amt", "fuel_pct",
                      "handling_amt", "handling_unit"):
                sv[k] = cfg.get(k)
        except Exception as exc:  # noqa: BLE001
            log.warning("service %s detail failed: %s", sv.get("service_id"), exc)
        if i % 10 == 0:
            log_progress(f"  …{i}/{len(active_services)}")
        time.sleep(0.35)

    _derive_active(snap)

    sid, iso = bq_writer.new_snapshot_id()
    snap.update({"snapshot_id": sid, "scraped_at": iso,
                 "duration_s": round(time.time() - t0, 1), "status": "ok"})
    bq_writer.write_snapshot(snap, source="ui-refresh")
    log_progress(f"wrote snapshot {sid}")

    return {
        "snapshot_id": sid, "duration_s": snap["duration_s"],
        "carriers": len(snap["carriers"]), "categories": len(snap["categories"]),
        "options": len(snap["options"]), "services": len(snap["services"]),
        "mapping": len(snap["mapping"]),
    }
