"""Startrack Freight Calculator — data services.

Phase 1 (Panel 1): fetch a single product's shipping-relevant fields from
BigQuery ``netocssv2.Products`` and present them in a digestible shape. Later
panels (Neto computed quote, live carrier quotes, historic) build on this.
"""
from __future__ import annotations

import json
import logging

PROJECT = "chainsawspares-385722"
log = logging.getLogger(__name__)


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
