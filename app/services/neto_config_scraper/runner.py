"""Run a full Neto Advanced-Configuration scrape and write a BQ snapshot.

Invoked by the NETO Advanced Config "Update" button (background thread). Logs in
once (reusing the shipping scraper's cPanel session), pages through the variable
list (max=250/page), then fetches each variable's detail page for its full
description, value-editor data type, and enum options.

cPanel creds: env NETO_CPANEL_USERNAME / NETO_CPANEL_PASSWORD, with a Secret
Manager fallback (neto-cpanel-username / neto-cpanel-password).
"""
from __future__ import annotations

import logging
import os
import time

from . import parse as P
from . import bq_writer
# reuse the proven cPanel login/takeover session from the shipping scraper
from app.services.neto_ship_scraper.session import create_session, BASE_URL

log = logging.getLogger(__name__)

LIST_URL = f"{BASE_URL}/config?item=config&max={{max}}&pagenum={{pg}}"
DETAIL_URL = f"{BASE_URL}/config/view?id={{id}}&mod={{mod}}"
PAGE_SIZE = 250
MAX_PAGES = 60  # safety cap (~15k vars); Neto wraps past the last page
DETAIL_SLEEP = 0.2


def _creds() -> tuple[str, str]:
    u, p = os.environ.get("NETO_CPANEL_USERNAME"), os.environ.get("NETO_CPANEL_PASSWORD")
    if u and p:
        return u, p
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


def _scrape_list(s, log_progress) -> list[dict]:
    """Page through the config list until a short page (the last one)."""
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for pg in range(1, MAX_PAGES + 1):
        html = s.get(LIST_URL.format(max=PAGE_SIZE, pg=pg)).text
        page_rows = P.parse_list(html)
        # stop if Neto wrapped back to an already-seen page
        new = [r for r in page_rows if (r["mod"], r["config_id"]) not in seen]
        for r in new:
            seen.add((r["mod"], r["config_id"]))
        rows.extend(new)
        log_progress(f"list page {pg}: +{len(new)} ({len(rows)} total)")
        if len(page_rows) < PAGE_SIZE or not new:
            break
        time.sleep(0.3)
    return rows


def run_scrape(progress=None) -> dict:
    """Full scrape -> BigQuery snapshot. Returns a summary dict."""
    def log_progress(msg):
        log.info("[config-scrape] %s", msg)
        if progress:
            progress(msg)

    t0 = time.time()
    user, pwd = _creds()
    log_progress("logging in to cPanel…")
    s = create_session(user, pwd)

    log_progress("fetching config list…")
    variables = _scrape_list(s, log_progress)
    total = len(variables)
    log_progress(f"fetching {total} variable details…")

    for i, v in enumerate(variables, 1):
        try:
            html = s.get(DETAIL_URL.format(id=v["config_id"], mod=v["mod"])).text
            det = P.parse_detail(html)
            v["data_type"] = det["data_type"]
            v["description"] = det["description"]
            v["options"] = det["options"]
            v["detail_ok"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("config detail %s/%s failed: %s", v["mod"], v["config_id"], exc)
            v.setdefault("data_type", None)
            v.setdefault("description", None)
            v.setdefault("options", [])
            v["detail_ok"] = False
        if i % 50 == 0:
            log_progress(f"  …details {i}/{total}")
        time.sleep(DETAIL_SLEEP)

    sid, iso = bq_writer.new_snapshot_id()
    snapshot = {
        "snapshot_id": sid, "scraped_at": iso, "vars": variables,
        "duration_s": round(time.time() - t0, 1), "status": "ok",
    }
    bq_writer.write_snapshot(snapshot, source="ui-refresh")
    n_detail_ok = sum(1 for v in variables if v.get("detail_ok"))
    log_progress(f"wrote snapshot {sid} ({total} vars, {n_detail_ok} with detail)")

    return {
        "snapshot_id": sid, "duration_s": snapshot["duration_s"],
        "n_vars": total, "n_detail_ok": n_detail_ok,
        "n_modules": len({v.get("module") for v in variables if v.get("module")}),
    }
