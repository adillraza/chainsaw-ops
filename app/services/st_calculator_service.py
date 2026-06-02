"""Startrack Freight Calculator — data services.

Phase 1 (Panel 1): fetch a single product's shipping-relevant fields from
BigQuery ``netocssv2.Products`` and present them in a digestible shape. Later
panels (Neto computed quote, live carrier quotes, historic) build on this.
"""
from __future__ import annotations

import json
import logging
import time

PROJECT = "chainsawspares-385722"
log = logging.getLogger(__name__)

# postcode -> [{suburb, state}], built once per process from our shipping
# history (carrier-validated pairs). Refreshed if older than _PC_TTL.
_pc_map: dict | None = None
_pc_built_at: float = 0.0
_PC_TTL = 12 * 3600


def _bq():
    from app.services.purchase_orders_service import purchase_orders_service
    return purchase_orders_service.client


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _main_image(images_json):
    """Pick the 'Main' image URL from the Images JSON column."""
    if not images_json:
        return None
    try:
        arr = images_json if isinstance(images_json, list) else json.loads(images_json)
    except (TypeError, ValueError):
        return None
    if not arr:
        return None
    for img in arr:
        if (img.get("Name") or "").lower() == "main" and img.get("URL"):
            return img["URL"]
    return arr[0].get("URL")


def _category_names() -> dict:
    """category_id -> friendly name, from the local neto_shipping mirror."""
    try:
        from app.services import neto_shipping_service as ns
        snap = ns.get_local()
        if snap:
            return {str(c.get("category_id")): c.get("name") for c in snap.get("categories", [])}
    except Exception:  # noqa: BLE001
        log.warning("could not load category names from neto_shipping mirror", exc_info=True)
    return {}


def _build_postcode_map() -> dict:
    """postcode -> ordered list of {suburb, state} from our shipping history
    (neto_orders ShipCity + Startrack invoice Receiver_Location). Carrier-
    validated pairs; most-frequent suburb first."""
    client = _bq()
    if client is None:
        raise RuntimeError("BigQuery client unavailable")
    sql = f"""
        SELECT pc, suburb, ANY_VALUE(state) AS state, SUM(freq) AS freq FROM (
          SELECT ShipPostCode AS pc, UPPER(TRIM(ShipCity)) AS suburb,
                 UPPER(TRIM(ShipState)) AS state, COUNT(*) AS freq
          FROM `{PROJECT}.dataform.neto_orders`
          WHERE ShipPostCode IS NOT NULL AND ShipPostCode != ''
            AND ShipCity IS NOT NULL AND TRIM(ShipCity) != ''
            AND ShipCountry IN ('AU','Australia')
          GROUP BY 1,2,3
          UNION ALL
          SELECT LPAD(CAST(Receiver_Postcode AS STRING), 4, '0') AS pc,
                 UPPER(TRIM(Receiver_Location)) AS suburb, CAST(NULL AS STRING) AS state, COUNT(*) AS freq
          FROM `{PROJECT}.startrack._all_invoices`
          WHERE Receiver_Postcode IS NOT NULL
            AND Receiver_Location IS NOT NULL AND TRIM(Receiver_Location) != ''
          GROUP BY 1,2
        )
        WHERE suburb NOT LIKE '%FUTILE%'
        GROUP BY pc, suburb
        ORDER BY pc, freq DESC
    """
    out: dict = {}
    for r in client.query(sql).result():
        pc = str(r["pc"]).strip()
        out.setdefault(pc, []).append({"suburb": r["suburb"], "state": r["state"]})
    return out


def suburbs_for_postcode(pc: str) -> list[dict]:
    """Return [{suburb, state}] for a postcode (most-shipped first)."""
    global _pc_map, _pc_built_at
    if _pc_map is None or (time.time() - _pc_built_at) > _PC_TTL:
        _pc_map = _build_postcode_map()
        _pc_built_at = time.time()
    return _pc_map.get((pc or "").strip(), [])


def parcel_items(product: dict, qty: int) -> list[dict]:
    """Build carrier line-items from the product. One (synthetic) box per unit.
    For multi-carton products we synthesize a box whose volume matches the
    stored cubic so the carrier's cubic-weight is right (length stays the real
    primary length so length limits still apply)."""
    L = (product.get("ship_l_cm") or 0) / 100.0
    W = (product.get("ship_w_cm") or 0) / 100.0
    H = (product.get("ship_h_cm") or 0) / 100.0
    wt = product.get("ship_weight_kg") or 0.1
    cubic = product.get("cubic_m3")
    if product.get("multi_carton") and cubic and L > 0 and W > 0:
        H = cubic / (L * W)
    item = {
        "length_cm": round(L * 100, 1) or 1.0,
        "width_cm": round(W * 100, 1) or 1.0,
        "height_cm": round(H * 100, 1) or 1.0,
        "weight_kg": round(wt, 3) or 0.1,
    }
    return [dict(item) for _ in range(max(1, int(qty or 1)))]


def _service_carrier_product(svc_name: str):
    """Map a Neto service name to (carrier_family, carrier_product_id)."""
    n = (svc_name or "").lower()
    if "startrack" in n or "star track" in n:
        if "fixed price" in n or "fpp" in n:
            return ("startrack", "FPP")
        if "premium" in n:
            return ("startrack", "PRM")
        return ("startrack", "EXP")  # Road Freight / default
    if "auspost" in n or "australia post" in n or "eparcel" in n:
        if "express" in n:
            return ("auspost", "7I85")
        return ("auspost", "7C85")
    return (None, None)


def neto_quotes(product: dict, qty: int, postcode: str, suburb: str) -> dict:
    """Compute the Neto quote(s) for the item: all services its ShippingCategory
    routes to, each priced as carrier_base × (1+fuel%) + handling × cartons,
    using the live neto_shipping config. Also returns the raw carrier quotes."""
    from app.services import neto_shipping_service as ns
    from app.services import carrier_quote_service as cq

    items = parcel_items(product, qty)
    carriers = cq.get_carrier_quotes(items, postcode, suburb)

    snap = ns.get_local() or {}
    services_cfg = {s.get("name"): s for s in snap.get("services", [])}
    # service names longest-first, to resolve the clean name from the messy
    # ship-page cell text ("<service name> <carrier pricing description…>").
    svc_names = sorted((n for n in services_cfg if n), key=len, reverse=True)
    cat_name = product.get("shipping_category_name")

    def resolve_service(text):
        t = (text or "").strip()
        for n in svc_names:
            if t.startswith(n):
                return n
        for n in svc_names:
            if n in t:
                return n
        return None

    def base_for(fam, pid):
        r = carriers.get(fam) or {}
        if not r.get("available"):
            return None, r.get("message")
        q = next((x for x in r["quotes"] if x["product_id"] == pid), None) or (r["quotes"][0] if r["quotes"] else None)
        return (q["total"] if q else None), None

    seen, results = set(), []
    for m in snap.get("mapping", []):
        if not m.get("block_active") or (m.get("category") or "") != cat_name:
            continue
        svc = resolve_service(m.get("service"))
        if not svc or svc in seen:
            continue
        seen.add(svc)
        # Skip non-freight (Pickup, Electronic Delivery) and international
        # services — this is a domestic parcel calculator.
        fam, pid = _service_carrier_product(svc)
        if not fam or "international" in svc.lower():
            continue
        cfg = services_cfg.get(svc, {})
        base, msg = base_for(fam, pid)
        if base is None:
            results.append({"service": svc, "carrier_family": fam, "available": False, "message": msg})
            continue
        fuel = cfg.get("fuel_pct") or 0
        handling = cfg.get("handling_amt") or 0
        cartons = max(1, int(qty or 1))
        fuel_line = round(base * fuel / 100.0, 2)
        handling_line = round(handling * cartons, 2)
        total = base + fuel_line + handling_line
        mn, mx = cfg.get("min_charge"), cfg.get("max_charge")
        if mn and total < mn:
            total = mn
        if mx and total > mx:
            total = mx
        results.append({
            "service": svc, "carrier_family": fam, "carrier_product": pid, "available": True,
            "base": round(base, 2), "fuel_pct": fuel, "fuel_line": fuel_line,
            "handling": handling, "cartons": cartons, "handling_line": handling_line,
            "total": round(total, 2),
        })

    results.sort(key=lambda r: (not r.get("available"), r.get("total") or 9e9))
    return {"carriers": carriers, "neto": results, "multi_carton": product.get("multi_carton")}


def get_product(sku: str) -> dict | None:
    """Return shipping-relevant fields for one SKU, or None if not found."""
    if not sku or not sku.strip():
        return None
    sku = sku.strip()
    client = _bq()
    if client is None:
        raise RuntimeError("BigQuery client unavailable")

    from google.cloud import bigquery
    q = f"""
        SELECT SKU, Name, Images, ShippingCategory, RequiresPackaging, IsActive,
               ShippingLength, ShippingWidth, ShippingHeight, ShippingWeight, CubicWeight,
               ItemLength, ItemWidth, ItemHeight, DefaultPrice, InventoryID
        FROM `{PROJECT}.netocssv2.Products`
        WHERE SKU = @sku
        LIMIT 1
    """
    job = client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("sku", "STRING", sku)]))
    rows = list(job.result())
    if not rows:
        return None
    r = dict(rows[0])

    sl, sw, sh = _f(r.get("ShippingLength")), _f(r.get("ShippingWidth")), _f(r.get("ShippingHeight"))
    cubic = _f(r.get("CubicWeight"))
    bbox = (sl or 0) * (sw or 0) * (sh or 0)
    # CubicWeight stores the SUM of all cartons' volumes; if it exceeds the
    # single bounding box, the product ships in more than one carton.
    multi_carton = bool(cubic and bbox and cubic > bbox * 1.02) or bool(cubic and not bbox)

    cat_id = str(r.get("ShippingCategory") or "")
    cat_name = _category_names().get(cat_id)

    return {
        "sku": r.get("SKU"),
        "name": r.get("Name"),
        "image_url": _main_image(r.get("Images")),
        "is_active": str(r.get("IsActive")) == "True",
        "default_price": _f(r.get("DefaultPrice")),
        "inventory_id": r.get("InventoryID"),
        # shipping (metres -> cm for display)
        "ship_l_cm": round(sl * 100, 1) if sl else None,
        "ship_w_cm": round(sw * 100, 1) if sw else None,
        "ship_h_cm": round(sh * 100, 1) if sh else None,
        "ship_weight_kg": _f(r.get("ShippingWeight")),
        "cubic_m3": cubic,
        "cubic_weight_kg": round(cubic * 250, 2) if cubic else None,  # 250 kg/m3 convention
        "requires_packaging": str(r.get("RequiresPackaging")) == "True",
        "shipping_category_id": cat_id or None,
        "shipping_category_name": cat_name,
        "multi_carton": multi_carton,
    }
