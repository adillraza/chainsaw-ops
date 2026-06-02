"""Live carrier freight quotes (Startrack + AusPost eParcel).

Used by the Freight Calculator: given a parcel (items) and destination, pull
the real carrier cost so staff skip the manual carrier calculators. Returns a
structured result per carrier with each product's quote, or a graceful
"can't ship" reason. Also the base the Neto-quote panel marks up.

Creds: Secret Manager `startrack-api-creds` / `eparcel-api-creds` (JSON
{key,password,account}); env override STARTRACK_API_CREDS / EPARCEL_API_CREDS.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

PROJECT = "chainsawspares-385722"
ORIGIN = {"postcode": "3356", "state": "VIC", "suburb": "DELACOMBE"}
ST_URL = "https://digitalapi.auspost.com.au/shipping/v1/prices/shipments"
EP_URL = "https://digitalapi.auspost.com.au/shipping/v1/prices/items"
AUSPOST_MAX_LEN_CM = 105.0

log = logging.getLogger(__name__)
_creds_cache: dict = {}

ST_PRODUCTS = {"EXP": "Road Express", "PRM": "Premium", "FPP": "Fixed Price Premium"}
EP_PRODUCTS = {"7C85": "Parcel Post + Sig", "7I85": "Express Post + Sig"}


def _creds(secret_name: str, env_key: str) -> dict:
    if secret_name in _creds_cache:
        return _creds_cache[secret_name]
    raw = os.environ.get(env_key)
    if not raw:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        n = f"projects/{PROJECT}/secrets/{secret_name}/versions/latest"
        raw = client.access_secret_version(name=n).payload.data.decode()
    creds = json.loads(raw)
    _creds_cache[secret_name] = creds
    return creds


def state_from_postcode(pc: str) -> str | None:
    if not pc:
        return None
    return {"0": "NT", "1": "NSW", "2": "NSW", "3": "VIC", "4": "QLD",
            "5": "SA", "6": "WA", "7": "TAS", "8": "VIC", "9": "QLD"}.get(pc.strip()[0])


def _post(url, creds, body):
    auth = "Basic " + base64.b64encode(f"{creds['key']}:{creds['password']}".encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": auth, "Account-Number": creds["account"],
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return None, {"_err": str(e)}


def _err_msg(body) -> str:
    errs = (body or {}).get("errors") or []
    return errs[0].get("message") if errs else (body.get("_err") or "carrier could not quote")


# ---------------------------------------------------------------------------
# Startrack
# ---------------------------------------------------------------------------
def startrack_quote(items: list[dict], to_pc: str, to_suburb: str, to_state: str) -> dict:
    creds = _creds("startrack-api-creds", "STARTRACK_API_CREDS")
    if not to_suburb:
        return {"carrier": "Startrack", "available": False, "message": "suburb required for Startrack", "quotes": []}
    quotes, last_err = [], None
    for pid, label in ST_PRODUCTS.items():
        body = {"shipments": [{
            "from": ORIGIN, "to": {"postcode": to_pc, "state": to_state, "suburb": to_suburb},
            "items": [{"length": it["length_cm"], "width": it["width_cm"], "height": it["height_cm"],
                       "weight": it["weight_kg"], "product_id": pid, "packaging_type": "CTN"} for it in items],
        }]}
        code, data = _post(ST_URL, creds, body)
        if code == 200 and data.get("shipments"):
            s = data["shipments"][0].get("shipment_summary", {})
            if s.get("total_cost") is not None:
                quotes.append({"product_id": pid, "label": label, "total": s["total_cost"],
                               "freight": s.get("freight_charge"), "fuel": s.get("fuel_surcharge"),
                               "gst": s.get("total_gst"), "security": s.get("security_surcharge")})
                continue
        last_err = _err_msg(data)
    if quotes:
        return {"carrier": "Startrack", "available": True, "quotes": quotes}
    return {"carrier": "Startrack", "available": False, "message": last_err or "no quote", "quotes": []}


# ---------------------------------------------------------------------------
# AusPost eParcel
# ---------------------------------------------------------------------------
def auspost_quote(items: list[dict], to_pc: str) -> dict:
    creds = _creds("eparcel-api-creds", "EPARCEL_API_CREDS")
    over = [it for it in items if (it.get("length_cm") or 0) > AUSPOST_MAX_LEN_CM]
    if over:
        return {"carrier": "AusPost", "available": False,
                "message": f"parcel length {over[0]['length_cm']}cm exceeds AusPost limit ({AUSPOST_MAX_LEN_CM:.0f}cm)",
                "quotes": []}
    # eParcel prices each article; total = sum across the qty items.
    totals: dict = {pid: 0.0 for pid in EP_PRODUCTS}
    ok = {pid: False for pid in EP_PRODUCTS}
    last_err = None
    for it in items:
        body = {"from": {"postcode": ORIGIN["postcode"]}, "to": {"postcode": to_pc},
                "items": [{"length": it["length_cm"], "width": it["width_cm"], "height": it["height_cm"],
                           "weight": it["weight_kg"], "product_ids": list(EP_PRODUCTS)}]}
        code, data = _post(EP_URL, creds, body)
        if code == 200 and data.get("items"):
            for p in data["items"][0].get("prices", []):
                pid = p.get("product_id")
                if pid in totals and p.get("calculated_price") is not None:
                    totals[pid] += p["calculated_price"]; ok[pid] = True
        else:
            last_err = _err_msg(data)
    quotes = [{"product_id": pid, "label": EP_PRODUCTS[pid], "total": round(totals[pid], 2)}
              for pid in EP_PRODUCTS if ok[pid]]
    if quotes:
        return {"carrier": "AusPost", "available": True, "quotes": quotes}
    return {"carrier": "AusPost", "available": False, "message": last_err or "no quote", "quotes": []}


def get_carrier_quotes(items: list[dict], to_pc: str, to_suburb: str) -> dict:
    """Both carriers for the parcel. Each entry is the startrack/auspost result dict."""
    state = state_from_postcode(to_pc)
    return {
        "state": state,
        "startrack": startrack_quote(items, to_pc, to_suburb, state),
        "auspost": auspost_quote(items, to_pc),
    }
