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


# storefront method name -> (carrier family, Startrack product_id for the live base)
_ST_PRODUCT = {"road": "EXP", "premium": "PRM", "fpp": "FPP"}


def _classify_method(method: str):
    """Map a storefront method name -> (family, tier)."""
    m = (method or "").lower()
    if "startrack" in m or "star track" in m:
        if "fixed" in m or "fpp" in m:
            return "startrack", "fpp"
        if "premium" in m:
            return "startrack", "premium"
        return "startrack", "road"  # Road Freight / default
    if "auspost" in m or "australia post" in m:
        return "auspost", ("express" if "express" in m else "standard")
    return None, None


def _service_matches(name: str, family: str, tier: str) -> bool:
    n = (name or "").lower()
    if family == "startrack":
        if "startrack" not in n and "star track" not in n:
            return False
        if tier == "road":
            return "road" in n and "skid" not in n
        if tier == "premium":
            return "premium" in n and "fixed" not in n
        if tier == "fpp":
            return "fixed" in n or "fpp" in n
    if family == "auspost":
        if not any(k in n for k in ("auspost", "australia post", "ap ")):
            return False
        if tier == "express":
            return "express" in n
        return "regular" in n or "standard" in n
    return False


def _attach_breakdowns(storefront: dict, carriers: dict, product: dict, qty: int, snap: dict) -> None:
    """Best-effort explanation of how each scraped Neto quote is composed, from
    the live shipping config. The scraped price stays authoritative; this just
    decomposes it (carrier/rate-table base + Neto fuel levy + handling).

    - Startrack services are "Third Party Shipping Rate" — base = the live
      Startrack API cost, marked up by the rate table's fuel% (+ handling). This
      reconciles to the cent.
    - AusPost services here are "Weight / Cubic" — Neto prices from an internal
      rate table (NOT the live AusPost API), so we can't source the base; we back
      it out from the scraped price given the configured fuel%/handling, and flag
      it as a Neto rate-table figure.
    """
    if not storefront or not storefront.get("options"):
        return
    services_cfg = {s["name"]: s for s in snap.get("services", []) if s.get("name")}
    svc_names = sorted(services_cfg, key=len, reverse=True)
    cat = product.get("shipping_category_name")

    def resolve(text):
        t = (text or "").strip()
        for n in svc_names:
            if t.startswith(n):
                return n
        for n in svc_names:
            if n in t:
                return n
        return None

    # clean service names routed to this product's category (active blocks)
    cat_services: list[str] = []
    for m in snap.get("mapping", []):
        if m.get("block_active") and (m.get("category") or "") == cat:
            n = resolve(m.get("service"))
            if n and n not in cat_services:
                cat_services.append(n)

    def pick_service(family, tier):
        for n in cat_services:                      # prefer this category's routing
            if _service_matches(n, family, tier):
                return services_cfg.get(n)
        for n in svc_names:                         # fallback: any matching service
            if _service_matches(n, family, tier):
                return services_cfg.get(n)
        return None

    cartons = max(1, int(qty or 1))
    for opt in storefront["options"]:
        family, tier = _classify_method(opt.get("method"))
        price = opt.get("price")
        bd = {"family": family, "tier": tier, "price": price}
        svc = pick_service(family, tier) if family else None
        if not svc or price is None:
            opt["breakdown"] = None
            continue
        fuel = svc.get("fuel_pct") or 0
        handling = svc.get("handling_amt") or 0
        ctype = svc.get("charge_type") or ""
        handling_line = round(handling * cartons, 2)
        bd.update({
            "service": svc.get("name"), "charge_type": ctype,
            "fuel_pct": fuel, "handling": handling, "cartons": cartons,
            "handling_line": handling_line,
        })
        if ctype == "Third Party Shipping Rate":
            # base = live carrier quote for the matched product
            bd["source"] = "carrier_api"
            r = carriers.get(family) or {}
            pid = _ST_PRODUCT.get(tier)
            base = None
            if r.get("available"):
                q = next((x for x in r["quotes"] if x["product_id"] == pid), None)
                base = q["total"] if q else None
            if base is not None:
                fuel_line = round(base * fuel / 100.0, 2)
                recon = round(base + fuel_line + handling_line, 2)
                bd.update({"base": round(base, 2), "fuel_line": fuel_line,
                           "recon": recon, "reconciles": abs(recon - price) < 0.02})
            else:
                bd.update({"base": None, "fuel_line": None, "recon": None,
                           "reconciles": False, "base_note": "live carrier base unavailable"})
        else:
            # Neto internal weight/cubic rate table — back the base out of the price
            bd["source"] = "neto_rate_table"
            base = round((price - handling_line) / (1 + fuel / 100.0), 2) if (1 + fuel / 100.0) else None
            bd.update({
                "base": base,
                "fuel_line": round(base * fuel / 100.0, 2) if base is not None else None,
                "recon": price, "reconciles": True,
            })
        opt["breakdown"] = bd


def neto_quotes(product: dict, qty: int, postcode: str, suburb: str) -> dict:
    """Panel data for a destination:

    - ``storefront``: the REAL Neto quote, read live from the public product
      page's Calculate Shipping widget (Panel 2). This is what the customer
      actually sees. Each option is enriched with a best-effort ``breakdown``
      (which Neto service/rate table, carrier base, fuel levy, handling) so staff
      can see *how* the price is composed — the scraped price stays authoritative.
    - ``carriers``: live carrier costs (what Startrack / AusPost charge us) for
      Panels 3 & 4.
    """
    from app.services import carrier_quote_service as cq
    from app.services import neto_storefront_service as sf
    from app.services import neto_shipping_service as ns

    items = parcel_items(product, qty)
    carriers = cq.get_carrier_quotes(items, postcode, suburb)
    state = cq.state_from_postcode(postcode)
    storefront = sf.storefront_quotes(product.get("sku"), qty, postcode, suburb, state)

    try:
        snap = ns.get_local() or {}
        _attach_breakdowns(storefront, carriers, product, qty, snap)
    except Exception:  # noqa: BLE001
        log.warning("storefront breakdown enrichment failed", exc_info=True)

    return {
        "carriers": carriers,
        "storefront": storefront,
        "multi_carton": product.get("multi_carton"),
    }


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
