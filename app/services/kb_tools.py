"""Live-data tools the KB chat (Gemini) can call when answering.

Each function is registered with Vertex AI as a ``FunctionDeclaration``;
the model picks which to call based on the agent's question. They all
return a flat JSON-serialisable dict — the model reads that as the
"tool result" and weaves it into the streamed answer.

Four tools:

* ``get_stock_and_price(sku)`` — combined Neto online + REX Ballarat
  retail availability, prices, warehouse breakdown.
* ``get_customer_summary(phone, email)`` — cached customer 360 row
  (lifetime value, badge, last order date).
* ``get_customer_orders(phone, email, limit)`` — recent orders from
  the cached customer 360 row.
* ``list_products(...)`` — enumerate the catalog by filter (fits_model,
  product_type, brand, in_stock_online_only). Used when the agent asks
  "give me all X" type questions — vector retrieval caps at 10 hits and
  isn't designed for enumeration. Returns top-N by stock + a count of
  total matches + a chainsawspares.com.au browse URL for the full list.

Data sources:
- Neto product: ``dataform.neto_product_list`` (Fivetran ~10min lag)
- REX retail: ``dataform.rex_ballarat_inventory`` (Dataform refresh)
- Customer: SQLite ``cached_customer_360`` (the same cache the live
  Customer 360 card uses, so tool answers match what the agent sees)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from google.cloud import bigquery

PROJECT = "chainsawspares-385722"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bq() -> bigquery.Client:
    from app.services.purchase_orders_service import purchase_orders_service
    return purchase_orders_service.client


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalise_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    from app.services.customer_360_service import normalize_phone
    p = normalize_phone(raw)
    return p or None


# ---------------------------------------------------------------------------
# get_stock_and_price
# ---------------------------------------------------------------------------

def get_stock_and_price(sku: str) -> dict[str, Any]:
    """Return current stock + price for ``sku`` across Neto online and REX
    Ballarat retail. Either side may be missing — returns whichever's matched.
    """
    if not sku:
        return {"matched": False, "reason": "no sku provided"}
    sku = sku.strip()

    sql = f"""
    WITH neto AS (
      SELECT
        SKU,
        Name,
        Brand,
        ItemURL,
        SAFE_CAST(AvailableSellQuantity AS INT64) AS available_online,
        SAFE_CAST(DefaultPrice   AS NUMERIC) AS default_price,
        SAFE_CAST(PromotionPrice AS NUMERIC) AS promo_price,
        PromotionStartDate,
        PromotionExpiryDate,
        WarehouseQuantity
      FROM `{PROJECT}.dataform.neto_product_list`
      WHERE SKU = @sku AND Approved = 'True' AND IsActive = 'True'
      LIMIT 1
    ),
    rex AS (
      SELECT
        manufacturer_sku,
        short_description,
        supplier_name,
        product_type_name,
        SAFE_CAST(available AS INT64) AS available_retail,
        SAFE_CAST(sell_price_inc AS NUMERIC) AS sell_price_inc,
        package
      FROM `{PROJECT}.dataform.rex_ballarat_inventory`
      WHERE manufacturer_sku = @sku
      LIMIT 1
    )
    SELECT
      COALESCE(neto.SKU, rex.manufacturer_sku)        AS sku,
      COALESCE(neto.Name, rex.short_description)      AS name,
      neto.SKU IS NOT NULL                            AS in_neto,
      rex.manufacturer_sku IS NOT NULL                AS in_rex,
      neto.Brand                                      AS brand,
      neto.ItemURL                                    AS neto_item_url,
      neto.available_online,
      neto.default_price,
      neto.promo_price,
      neto.PromotionStartDate                         AS promo_start,
      neto.PromotionExpiryDate                        AS promo_end,
      neto.WarehouseQuantity                          AS warehouse_quantity_json,
      rex.available_retail,
      rex.sell_price_inc                              AS rex_sell_price_inc,
      rex.supplier_name                               AS rex_supplier,
      rex.product_type_name                           AS rex_type,
      rex.package                                     AS rex_is_kit
    FROM neto
    FULL OUTER JOIN rex ON neto.SKU = rex.manufacturer_sku
    """
    job = _bq().query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("sku", "STRING", sku),
        ]),
    )
    row = next(iter(job.result()), None)
    if row is None:
        return {"matched": False, "sku": sku,
                "reason": "SKU not found in Neto or REX Ballarat"}

    # Per-warehouse Neto breakdown — the JSON column varies in shape.
    warehouses: dict[str, int] = {}
    raw = row.warehouse_quantity_json
    try:
        data = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        WH_NAMES = {"1": "Kennedy's", "2": "Ballarat"}
        for w in (data or []):
            wid = str(w.get("WarehouseID"))
            qty = int(w.get("Quantity") or 0)
            warehouses[WH_NAMES.get(wid, f"WH-{wid}")] = qty
    except Exception:
        pass

    today = None  # active promo if today is between start and end
    try:
        from datetime import date
        today = date.today()
    except Exception:
        pass
    promo_active = False
    if row.promo_price is not None and row.promo_start and row.promo_end and today:
        try:
            promo_active = (row.promo_start.date() if hasattr(row.promo_start, "date") else row.promo_start) <= today <= (row.promo_end.date() if hasattr(row.promo_end, "date") else row.promo_end)
        except Exception:
            promo_active = False

    out: dict[str, Any] = {
        "matched": True,
        "sku": row.sku,
        "name": row.name,
        "brand": row.brand,
    }
    if row.in_neto:
        out["online"] = {
            "available": row.available_online,
            "default_price_aud": _to_float(row.default_price),
            "promo_price_aud":   _to_float(row.promo_price) if promo_active else None,
            "promo_period":      f"{row.promo_start} to {row.promo_end}" if promo_active else None,
            "warehouses":        warehouses,
            "url":               f"https://www.chainsawspares.com.au/{row.neto_item_url}" if row.neto_item_url else None,
        }
    if row.in_rex:
        out["retail_ballarat"] = {
            "available":          row.available_retail,
            "sell_price_inc_aud": _to_float(row.rex_sell_price_inc),
            "supplier":           row.rex_supplier,
            "product_type":       row.rex_type,
            "is_kit":             bool(row.rex_is_kit),
        }
    return out


# ---------------------------------------------------------------------------
# get_customer_summary
# ---------------------------------------------------------------------------

def get_customer_summary(phone: str | None = None,
                         email: str | None = None) -> dict[str, Any]:
    """Quick customer profile — name, badge, lifetime totals, last activity.

    Reads from the cached_customer_360 SQLite table (same data the live
    Customer 360 card uses) so answers are consistent with what the agent
    sees on screen. Hourly-fresh.
    """
    if not phone and not email:
        return {"matched": False, "reason": "phone or email required"}

    customers = _resolve_customers(phone=phone, email=email)
    if not customers:
        return {"matched": False, "phone": phone, "email": email}

    primary = customers[0]
    return {
        "matched": True,
        "name": _full_name(primary),
        "primary_email": primary.get("email"),
        "secondary_email": primary.get("secondary_email"),
        "matched_records": len(customers),
        "lifetime_orders":     primary.get("lifetime_order_count"),
        "lifetime_value_aud":  _to_float(primary.get("lifetime_value")),
        "avg_order_value_aud": _to_float(primary.get("avg_order_value")),
        "customer_since":      str(primary.get("customer_since") or ""),
        "last_order_date":     str(primary.get("last_order_date") or ""),
        "days_since_last_order": primary.get("days_since_last_order"),
        "customer_badge":      primary.get("customer_badge"),
        "lifetime_rma_count":  primary.get("lifetime_rma_count"),
    }


# ---------------------------------------------------------------------------
# get_customer_orders
# ---------------------------------------------------------------------------

def get_customer_orders(phone: str | None = None,
                        email: str | None = None,
                        limit: int = 5) -> dict[str, Any]:
    """Recent orders for the customer — date, OrderID, total, line items.

    Use this when the agent asks "what has this customer ordered before"
    or "have they bought X". Bounded to the last ``limit`` orders to keep
    the model context tight (default 5).
    """
    if not phone and not email:
        return {"matched": False, "reason": "phone or email required"}

    customers = _resolve_customers(phone=phone, email=email)
    if not customers:
        return {"matched": False, "phone": phone, "email": email}

    primary = customers[0]
    recent = primary.get("recent_orders") or []
    out_orders = []
    for o in recent[: max(1, min(20, int(limit)))]:
        out_orders.append({
            "order_id":   o.get("order_id"),
            "date":       str(o.get("order_date") or ""),
            "status":     o.get("order_status"),
            "total_aud":  _to_float(o.get("total")),
            "lines": [
                {"sku": l.get("sku"), "qty": l.get("qty"),
                 "name": l.get("name"), "unit_price": _to_float(l.get("unit_price"))}
                for l in (o.get("lines") or [])[:8]
            ],
        })
    return {
        "matched": True,
        "name": _full_name(primary),
        "lifetime_orders": primary.get("lifetime_order_count"),
        "orders": out_orders,
    }


# ---------------------------------------------------------------------------
# list_products
# ---------------------------------------------------------------------------
#
# Why this tool exists, in plain English:
#
# The default KB retrieval path is vector search over kb.documents, capped
# at TOP_K_SOURCES = 10. That's the right surface for "what's the right
# chain for this bar" / "does product X fit Y" questions — semantically
# tight, similarity-ranked. It is the WRONG surface for "give me all
# bars for the MS660", where the catalogue genuinely has 200+ matches
# and the user wants enumeration, not recommendation. With TOP_K=10 the
# agent only sees ~3-4% of the available products and assumes that's
# the full list.
#
# ``list_products`` is the structured-filter counterpart. It runs a
# direct SQL filter on ``dataform.neto_product_list`` — no embeddings,
# no similarity ranking — and returns a representative top-N ordered by
# online stock (so well-stocked options surface first), plus the total
# count of matches and a chainsawspares.com.au search URL the agent can
# hand to the customer to browse the full list.

# The default sort. "In-stock first" so the top-N is genuinely useful;
# zero-stock and discontinued products rank below. Within an in-stock
# tier rows are ordered by SKU so output is stable across calls.
_LIST_ORDER_BY = (
    "SAFE_CAST(AvailableSellQuantity AS INT64) > 0 DESC, "
    "SAFE_CAST(AvailableSellQuantity AS INT64) DESC NULLS LAST, "
    "SKU ASC"
)


def list_products(fits_model: str | None = None,
                  product_type: str | None = None,
                  brand: str | None = None,
                  in_stock_online_only: bool = False,
                  limit: int = 20) -> dict[str, Any]:
    """Enumerate products in the catalogue matching the given filters.

    All filters are optional, but at least one should be provided —
    calling with everything-None would return "top N of the whole
    catalogue" which is useless. The function returns:

    * ``total_matched``   — full COUNT(*) of rows passing the filters
    * ``returned``        — len(products) ≤ ``limit``
    * ``products``        — list of dicts (sku, name, brand, stock, prices, url)
    * ``filters_applied`` — echoes the filter values back for traceability
    * ``browse_url``      — chainsawspares.com.au search URL the agent
                            can give the customer for the full list
    """
    # Soft clamp — the agent might pass weird values. 30 is a reasonable
    # max for chat-pane display; beyond that send them to ``browse_url``.
    try:
        limit = max(1, min(30, int(limit or 20)))
    except (TypeError, ValueError):
        limit = 20

    if not any([fits_model, product_type, brand]):
        return {
            "matched": False,
            "reason": "At least one filter (fits_model, product_type, or brand) is required.",
        }

    where = ["Approved = 'True'", "IsActive = 'True'"]
    params: list[bigquery.ScalarQueryParameter] = []

    if fits_model:
        # Split on whitespace so multi-word models ("Stihl MS660",
        # "Husqvarna 445") match products that use ANY form of the
        # phrase — "066 MS660", "MS660 044", "Husq. 445", etc. — not
        # just an exact substring. Require EVERY token to appear
        # somewhere in the searchable text fields.
        # We special-case single-character tokens (skip them) to avoid
        # matching every product on stray "a"/"e" particles.
        # Stop-words: brand names that are too generic on their own
        # should still match BUT we also have an OR clause so "Stihl"
        # alone matches "Stihl" products. Acceptable trade-off.
        tokens = [t for t in fits_model.lower().split() if len(t) > 1]
        if not tokens:
            tokens = [fits_model.strip().lower()]
        token_clauses = []
        for i, tok in enumerate(tokens):
            pname = f"fm{i}"
            token_clauses.append(
                f"(LOWER(IFNULL(Name,'')) LIKE @{pname} "
                f"OR LOWER(IFNULL(Model,'')) LIKE @{pname} "
                f"OR LOWER(IFNULL(ModelNumber,'')) LIKE @{pname} "
                f"OR LOWER(IFNULL(Description,'')) LIKE @{pname} "
                f"OR LOWER(IFNULL(SearchKeywords,'')) LIKE @{pname})"
            )
            params.append(bigquery.ScalarQueryParameter(pname, "STRING", f"%{tok}%"))
        where.append("(" + " AND ".join(token_clauses) + ")")

    if product_type:
        # Match against Name (e.g., "Chainsaw Chain", "Guide Bar", "Spark
        # Plug") rather than the product_type column, which is the
        # kit/standalone taxonomy (not item category).
        #
        # Two common categories need special handling because chain
        # product names also mention "Bar" (in the fitment text like "for
        # Stihl 20\" Bar"). A naive LIKE '%bar%' for product_type='bar'
        # ends up returning hundreds of chains. So:
        #   * pt='bar'   → name has "bar" as a word AND name doesn't have
        #                  "chain" UNLESS it's a "combo" (bar+chain combos
        #                  are legitimately both)
        #   * pt='chain' → name has "chain"
        #   * everything else → substring match (the simple case)
        pt = product_type.strip().lower()
        if pt in ("bar", "bars", "guide bar"):
            # Bar products mention "bar" in their name. But chain
            # products ALSO often mention "bar" in their fitment text
            # ("for Stihl 20\" Bar"). Exclude items whose name also
            # contains "chain" UNLESS they're a "Bar & Chain Combo"
            # (which is legitimately both).
            where.append(
                "(LOWER(IFNULL(Name,'')) LIKE '%bar%' "
                "AND (LOWER(IFNULL(Name,'')) NOT LIKE '%chain%' "
                "     OR LOWER(IFNULL(Name,'')) LIKE '%combo%'))"
            )
        elif pt in ("chain", "chains"):
            where.append("LOWER(IFNULL(Name,'')) LIKE '%chain%'")
        else:
            where.append("LOWER(IFNULL(Name,'')) LIKE @pt")
            params.append(bigquery.ScalarQueryParameter("pt", "STRING", f"%{pt}%"))

    if brand:
        where.append("LOWER(IFNULL(Brand,'')) = @brand")
        params.append(bigquery.ScalarQueryParameter("brand", "STRING", brand.strip().lower()))

    if in_stock_online_only:
        where.append("SAFE_CAST(AvailableSellQuantity AS INT64) > 0")

    where_sql = " AND ".join(where)

    # Count + list in a single round-trip via WITH + UNION ALL would be
    # one extra layer of complexity for no real gain — Neto product list
    # is small (~6k active rows), so two cheap queries is fine.
    count_sql = (
        f"SELECT COUNT(*) AS total "
        f"FROM `{PROJECT}.dataform.neto_product_list` "
        f"WHERE {where_sql}"
    )
    list_sql = f"""
    SELECT
      SKU, Name, Brand, ItemURL, Model,
      SAFE_CAST(AvailableSellQuantity AS INT64) AS available_online,
      SAFE_CAST(DefaultPrice   AS NUMERIC) AS default_price,
      SAFE_CAST(PromotionPrice AS NUMERIC) AS promo_price
    FROM `{PROJECT}.dataform.neto_product_list`
    WHERE {where_sql}
    ORDER BY {_LIST_ORDER_BY}
    LIMIT @lim
    """

    try:
        total_job = _bq().query(
            count_sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        )
        total_matched = int(next(iter(total_job.result())).total)
    except Exception as exc:
        log.warning("list_products: count query failed: %s", exc)
        return {"matched": False, "reason": f"count query failed: {exc}"}

    list_params = params + [bigquery.ScalarQueryParameter("lim", "INT64", limit)]
    try:
        rows = list(_bq().query(
            list_sql,
            job_config=bigquery.QueryJobConfig(query_parameters=list_params),
        ).result())
    except Exception as exc:
        log.warning("list_products: list query failed: %s", exc)
        return {"matched": False, "reason": f"list query failed: {exc}"}

    products = []
    for r in rows:
        products.append({
            "sku":               r.SKU,
            "name":              r.Name,
            "brand":             r.Brand,
            "model":             r.Model,
            "available_online":  r.available_online,
            "default_price_aud": _to_float(r.default_price),
            "promo_price_aud":   _to_float(r.promo_price),
            "url": (f"https://www.chainsawspares.com.au/{r.ItemURL}"
                    if r.ItemURL else None),
        })

    # Build the "see all" browse URL. Chainsawspares' default search
    # endpoint is ``/?q=<terms>`` — verified 2026-05-26. Combine the
    # filter values into a single search string, URL-encoded.
    from urllib.parse import quote_plus
    search_bits = [b for b in (brand, fits_model, product_type) if b]
    browse_url = (
        "https://www.chainsawspares.com.au/?q="
        + quote_plus(" ".join(search_bits)) if search_bits else None
    )

    return {
        "matched": True,
        "total_matched": total_matched,
        "returned": len(products),
        "filters_applied": {
            "fits_model":            fits_model,
            "product_type":          product_type,
            "brand":                 brand,
            "in_stock_online_only":  in_stock_online_only,
        },
        "products":   products,
        "browse_url": browse_url,
    }


# ---------------------------------------------------------------------------
# Internal — customer resolution via the shared customer cache
# ---------------------------------------------------------------------------

def _resolve_customers(*, phone: str | None, email: str | None) -> list[dict]:
    """Return matching customer_360 rows. Order: phone first, then email."""
    from app.extensions import db
    from app.models.customer_cache import CachedCustomer360, CachedPhoneLookup

    customers: list[dict] = []
    seen: set[str] = set()

    if phone:
        ph = _normalise_phone(phone)
        if ph:
            pl = db.session.query(CachedPhoneLookup).filter_by(phone=ph).first()
            if pl:
                usernames = json.loads(pl.usernames_json) if pl.usernames_json else []
                if usernames:
                    rows = (db.session.query(CachedCustomer360)
                            .filter(CachedCustomer360.Username.in_(usernames))
                            .all())
                    for r in rows:
                        if r.Username in seen: continue
                        seen.add(r.Username)
                        customers.append(json.loads(r.payload_json))

    if email:
        em = email.strip().lower()
        if em:
            rows = (db.session.query(CachedCustomer360)
                    .filter(db.or_(
                        db.func.lower(CachedCustomer360.email) == em,
                        db.func.lower(CachedCustomer360.secondary_email) == em,
                    )).all())
            for r in rows:
                if r.Username in seen: continue
                seen.add(r.Username)
                customers.append(json.loads(r.payload_json))

    # Sort by lifetime value desc — the "primary" customer when multiple match.
    def _lv(c):
        v = c.get("lifetime_value")
        try: return float(v) if v else 0.0
        except (TypeError, ValueError): return 0.0
    customers.sort(key=_lv, reverse=True)
    return customers


def _full_name(c: dict) -> str:
    return ((c.get("name_first") or "").strip() + " "
            + (c.get("name_last") or "").strip()).strip() or None


# ---------------------------------------------------------------------------
# Tool registry — what kb_chat passes to Vertex
# ---------------------------------------------------------------------------

TOOL_DISPATCH = {
    "get_stock_and_price":  get_stock_and_price,
    "get_customer_summary": get_customer_summary,
    "get_customer_orders":  get_customer_orders,
    "list_products":        list_products,
}


def function_declarations():
    """Build Vertex FunctionDeclaration objects describing each tool.

    Lazy-imported because vertexai is heavy and only kb_chat needs this.
    """
    from vertexai.generative_models import FunctionDeclaration

    return [
        FunctionDeclaration(
            name="get_stock_and_price",
            description=(
                "Look up the current stock level and price for a product SKU "
                "across both the online store (Neto) and the Ballarat retail "
                "store (REX). Use this when the agent asks about availability, "
                "stock, price, or whether a specific item is in stock at a "
                "specific location. The SKU should come from the SOURCES list "
                "in the user message — don't invent one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sku": {
                        "type": "string",
                        "description": "Exact product SKU as it appears on chainsawspares.com.au, e.g. 'WH-7' or '325_063_67_SEMIx1_'.",
                    },
                },
                "required": ["sku"],
            },
        ),
        FunctionDeclaration(
            name="get_customer_summary",
            description=(
                "Get a quick profile of the current customer (name, lifetime "
                "value, badge, days since last order). Use only when the agent "
                "explicitly references the customer they're talking to. "
                "Either phone or email is required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Customer phone (any AU format — local 04..., international +61...)."},
                    "email": {"type": "string", "description": "Customer email address."},
                },
            },
        ),
        FunctionDeclaration(
            name="get_customer_orders",
            description=(
                "List the customer's recent orders (date, order ID, total, "
                "line items). Use when the agent asks 'has this customer "
                "ordered X before?' or 'what was their last order?'. Phone "
                "or email required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Customer phone."},
                    "email": {"type": "string", "description": "Customer email address."},
                    "limit": {"type": "integer", "description": "How many recent orders to return (default 5, max 20)."},
                },
            },
        ),
        FunctionDeclaration(
            name="list_products",
            description=(
                "Enumerate products in the catalogue by filter. Use this — "
                "NOT vector retrieval — when the agent asks for a list / "
                "category browse, e.g. 'give me all bars for the MS660', "
                "'what Hurricane chains do we have', 'list spark plugs that "
                "fit the Husqvarna 445'. Vector retrieval caps at 10 hits "
                "and is for similarity-ranked recommendations; this tool "
                "does a structured filter so the agent sees a true count "
                "of matches and a representative sample. Combine filters "
                "freely — at least one of fits_model, product_type, or "
                "brand is required. Returns up to ``limit`` products "
                "(sorted in-stock-first) plus the total match count and "
                "a chainsawspares.com.au browse URL the agent can give "
                "the customer for the full list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fits_model": {
                        "type": "string",
                        "description": "Saw / equipment model the product must fit. Free-text — matches against Name, Model, Description, SearchKeywords. Examples: 'MS660', 'Husqvarna 445', 'Stihl MS250'. Be specific; broad strings like 'chainsaw' will match thousands of products.",
                    },
                    "product_type": {
                        "type": "string",
                        "description": "Item category as it appears in the product name. Examples: 'bar', 'chain', 'spark plug', 'air filter', 'oil pump', 'bar oil', 'sprocket'. Substring-matched against Name (case-insensitive), so 'bar' will also match 'Bar & Chain Combo'.",
                    },
                    "brand": {
                        "type": "string",
                        "description": "Exact brand match. Examples: 'Hurricane', 'Carlton', 'Tsumura', 'Jakmax', 'JONO & JOHNO'. Use the brand strings from previous answers — case-insensitive equality.",
                    },
                    "in_stock_online_only": {
                        "type": "boolean",
                        "description": "If true, hide products with zero online stock. Useful when the customer needs something they can buy right now.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max products to return (default 20, capped at 30). The catalogue often has 100+ matches — total_matched will tell you, and browse_url has the full list.",
                    },
                },
            },
        ),
    ]
