"""Live-data tools the Customer 360 chat (Gemini) can call.

Unlike the KB chat tools (``kb_tools``), these are **bound to the
customer whose card the agent is looking at** — the model never has to
pass a phone or email, and it can't wander to a different customer. The
chat layer calls :func:`build_dispatch` once per request with the card
payload already loaded, and hands the returned dispatch map to Gemini.

Two kinds of tool:

* **Customer-scoped** (``get_customer_profile``, ``get_recent_orders``,
  ``get_calls``, ``get_call_detail``, ``get_related_accounts``) — read
  from the in-memory ``card`` payload (the exact dict the card UI
  renders, from ``customer_360_service.get_card``). No extra BQ.
* **Catalogue** (``get_stock_and_price``, ``list_products``) — delegate
  straight to :mod:`app.services.kb_tools` so there's one implementation.

Sensitivity: ``get_call_detail`` re-fetches the full call via the
service and applies :func:`redact_sensitive_call_details` when the
viewer lacks ``support.calls.view_sensitive`` — mirroring the
call-details modal exactly, so the AI can never surface a sensitive
call's transcript/summary/audio to someone who can't already see it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.services import kb_tools

log = logging.getLogger(__name__)

PROJECT = "chainsawspares-385722"


# ---------------------------------------------------------------------------
# Customer-scoped helpers (read from the loaded card payload)
# ---------------------------------------------------------------------------

def _full_name(c: dict) -> str | None:
    return ((c.get("name_first") or "").strip() + " "
            + (c.get("name_last") or "").strip()).strip() or None


def _primary(card: dict) -> dict | None:
    """The card's primary customer record (header account), or None."""
    customers = card.get("customers") or []
    return customers[0] if customers else None


def _customer_profile(card: dict) -> dict[str, Any]:
    primary = _primary(card)
    if not primary:
        return {"matched": False,
                "note": "Unknown caller — no Neto customer matches this number."}
    return {
        "matched": True,
        "name": _full_name(primary),
        "primary_email": primary.get("email"),
        "secondary_email": primary.get("secondary_email"),
        "matched_records": len(card.get("customers") or []),
        "lifetime_orders": primary.get("lifetime_order_count"),
        "lifetime_value_aud": primary.get("lifetime_value"),
        "avg_order_value_aud": primary.get("avg_order_value"),
        "customer_since": str(primary.get("customer_since") or ""),
        "last_order_date": str(primary.get("last_order_date") or ""),
        "days_since_last_order": primary.get("days_since_last_order"),
        "customer_badge": primary.get("customer_badge"),
        "lifetime_rma_count": primary.get("lifetime_rma_count"),
        "account_origin": primary.get("sales_channel") or primary.get("neto_type"),
    }


def _recent_orders(card: dict, limit: int = 5) -> dict[str, Any]:
    primary = _primary(card)
    if not primary:
        return {"matched": False, "note": "Unknown caller — no order history."}
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    orders = []
    for o in (primary.get("recent_orders") or [])[:limit]:
        orders.append({
            "order_id": o.get("order_id"),
            "date": str(o.get("order_date") or ""),
            "status": o.get("order_status"),
            "total_aud": o.get("total"),
            "lines": [
                {"sku": l.get("sku"), "qty": l.get("qty"),
                 "name": l.get("name"), "unit_price_aud": l.get("unit_price")}
                for l in (o.get("lines") or [])[:8]
            ],
        })
    return {"matched": True, "name": _full_name(primary),
            "lifetime_orders": primary.get("lifetime_order_count"),
            "orders": orders}


def _calls(card: dict, limit: int = 8) -> dict[str, Any]:
    """Call history totals + behaviour insights + the recent call list.

    Each recent call carries its ``session_id`` so the model can drill
    into a specific call via ``get_call_detail``.
    """
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 8
    h = card.get("call_history") or {}
    b = card.get("call_behavior") or {}
    recent = (h.get("recent_calls") or h.get("last_5_calls") or [])[:limit]
    calls = [{
        "session_id": c.get("session_id"),
        "when": str(c.get("call_time") or ""),
        "direction": c.get("direction"),
        "disposition": c.get("disposition"),
        "source": c.get("source"),
        "duration_seconds": c.get("duration_seconds"),
        "transferred": bool(c.get("is_transferred")),
    } for c in recent]
    last = b.get("last_call") or {}
    return {
        "total_calls": h.get("total_calls") or 0,
        "days_since_last_call": h.get("days_since_last_call"),
        "connected_total": h.get("connected_total") or 0,
        "missed_total": h.get("missed_total") or 0,
        "abandoned_total": h.get("abandoned_total") or 0,
        "voicemail_total": h.get("voicemail_total") or 0,
        "typical_call_types": b.get("typical_call_types") or b.get("top_call_types"),
        "top_reasons_for_call": b.get("top_reasons_for_call"),
        "top_problems": b.get("top_problems"),
        "most_recent_analysed_call": {
            "date": str(last.get("call_date") or ""),
            "agent": last.get("agent_name"),
            "sentiment": last.get("sentiment_label"),
            "summary": last.get("summary"),
        } if last else None,
        "recent_calls": calls,
    }


def _related_accounts(card: dict) -> dict[str, Any]:
    def _slim(item: dict) -> dict:
        cu = item.get("customer") or {}
        return {"name": _full_name(cu), "username": item.get("username"),
                "match_types": item.get("match_types") or item.get("match_type")}
    return {
        "linked_by_phone": len(card.get("usernames") or []),
        "by_email": [_slim(i) for i in (card.get("related_by_email") or [])],
        "by_address": [_slim(i) for i in (card.get("related_by_address") or [])],
        "guest_stubs": len(card.get("guest_stubs") or []),
    }


# ---------------------------------------------------------------------------
# Live BigQuery lookups — RMAs / refunds and full order+shipping detail.
# These query the warehouse directly (the cached card payload only carries
# a thin recent-orders summary and no RMA detail at all).
# ---------------------------------------------------------------------------

def _to_float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _rmas(usernames: list[str], limit: int = 10) -> dict[str, Any]:
    """RMAs / refunds for the customer, header + line detail (reason,
    resolution, per-line refund). Answers "what was the refund for/amount"."""
    if not usernames:
        return {"matched": False, "note": "Unknown caller — no RMAs on file."}
    from google.cloud import bigquery
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    sql = f"""
    SELECT
      r.RmaID, r.OrderID, r.RmaStatus, r.DateIssued, r.DateApproved,
      SAFE_CAST(r.RefundTotal    AS NUMERIC) AS refund_total,
      SAFE_CAST(r.RefundedTotal  AS NUMERIC) AS refunded_total,
      SAFE_CAST(r.RefundSubtotal AS NUMERIC) AS refund_subtotal,
      SAFE_CAST(r.ShippingRefundAmount AS NUMERIC) AS shipping_refund,
      r.InternalNotes,
      ARRAY(
        SELECT AS STRUCT l.SKU, l.ProductName, l.Quantity, l.ReturnReason,
               l.ResolutionOutcome, l.ResolutionStatus,
               SAFE_CAST(l.RefundAmount AS NUMERIC) AS line_refund
        FROM `{PROJECT}.dataform.neto_rma_lines` l WHERE l.RmaID = r.RmaID
      ) AS lines
    FROM `{PROJECT}.dataform.neto_rmas` r
    WHERE r.CustomerUsername IN UNNEST(@u)
    ORDER BY r.DateIssued DESC
    LIMIT @lim
    """
    job = kb_tools._bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("u", "STRING", usernames),
        bigquery.ScalarQueryParameter("lim", "INT64", limit),
    ]))
    rmas = []
    for r in job.result():
        rmas.append({
            "rma_id": r.RmaID, "order_id": r.OrderID, "status": r.RmaStatus,
            "date_issued": str(r.DateIssued or ""), "date_approved": str(r.DateApproved or ""),
            "refund_total_aud": _to_float(r.refund_total),
            "refunded_total_aud": _to_float(r.refunded_total),
            "refund_subtotal_aud": _to_float(r.refund_subtotal),
            "shipping_refund_aud": _to_float(r.shipping_refund),
            "internal_notes": r.InternalNotes,
            "lines": [{
                "sku": l["SKU"], "name": l["ProductName"], "qty": l["Quantity"],
                "return_reason": l["ReturnReason"], "resolution": l["ResolutionOutcome"],
                "resolution_status": l["ResolutionStatus"],
                "line_refund_aud": _to_float(l["line_refund"]),
            } for l in (r.lines or [])],
        })
    return {"matched": bool(rmas), "count": len(rmas), "rmas": rmas}


def _order_detail(usernames: list[str], order_id: str) -> dict[str, Any]:
    """Full detail for ONE order: status, totals breakdown, ship address +
    method, and per-line items WITH tracking. Scoped to this customer."""
    if not order_id:
        return {"error": "order_id required"}
    if not usernames:
        return {"matched": False, "note": "Unknown caller."}
    from google.cloud import bigquery
    sql = f"""
    SELECT
      OrderID, OrderStatus, CompleteStatus,
      SAFE_CAST(GrandTotal      AS NUMERIC) AS grand_total,
      SAFE_CAST(ProductSubtotal AS NUMERIC) AS product_subtotal,
      SAFE_CAST(ShippingTotal   AS NUMERIC) AS shipping_total,
      ShippingOption,
      ShipFirstName, ShipLastName, ShipStreetLine1, ShipStreetLine2,
      ShipCity, ShipState, ShipPostCode, ShipPhone,
      TO_JSON_STRING(OrderLine) AS order_line_json
    FROM `{PROJECT}.dataform.neto_orders`
    WHERE OrderID = @oid AND Username IN UNNEST(@u)
    LIMIT 1
    """
    job = kb_tools._bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("oid", "STRING", order_id.strip()),
        bigquery.ArrayQueryParameter("u", "STRING", usernames),
    ]))
    row = next(iter(job.result()), None)
    if row is None:
        return {"matched": False, "order_id": order_id,
                "note": "No order with that ID for this customer."}
    lines = []
    try:
        for l in (json.loads(row.order_line_json) if row.order_line_json else []):
            lines.append({
                "sku": l.get("SKU"), "name": l.get("ProductName"),
                "qty": l.get("Quantity"),
                "unit_price_aud": _to_float(l.get("UnitPrice")),
                "shipping_method": l.get("ShippingMethod"),
                "tracking": l.get("ShippingTracking") or None,
                "tracking_url": l.get("ShippingTrackingUrl") or None,
            })
    except (TypeError, ValueError):
        pass
    ship = " ".join(p for p in [
        (row.ShipFirstName or "") + " " + (row.ShipLastName or ""),
        row.ShipStreetLine1, row.ShipStreetLine2,
        row.ShipCity, row.ShipState, row.ShipPostCode] if p and p.strip()).strip()
    return {
        "matched": True, "order_id": row.OrderID,
        "status": row.OrderStatus, "complete_status": row.CompleteStatus,
        "grand_total_aud": _to_float(row.grand_total),
        "product_subtotal_aud": _to_float(row.product_subtotal),
        "shipping_total_aud": _to_float(row.shipping_total),
        "shipping_method": row.ShippingOption,
        "ship_to": ship or None, "ship_phone": row.ShipPhone,
        "lines": lines,
    }


def _orders_with_lines(usernames: list[str], limit: int = 5) -> dict[str, Any]:
    """Recent orders WITH line items, straight from neto_orders.

    The cached card payload's recent_orders omit line items for many
    customers (lines empty), so the model couldn't see products. This
    reads the warehouse so line detail is always present.
    """
    if not usernames:
        return {"matched": False, "note": "Unknown caller — no orders."}
    from google.cloud import bigquery
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    sql = f"""
    SELECT OrderID, OrderStatus, DatePlaced,
           SAFE_CAST(GrandTotal AS NUMERIC) AS grand_total,
           TO_JSON_STRING(OrderLine) AS lines_json
    FROM `{PROJECT}.dataform.neto_orders`
    WHERE Username IN UNNEST(@u)
    ORDER BY DatePlaced DESC
    LIMIT @lim
    """
    job = kb_tools._bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("u", "STRING", usernames),
        bigquery.ScalarQueryParameter("lim", "INT64", limit),
    ]))
    orders = []
    for row in job.result():
        lines = []
        try:
            for l in (json.loads(row.lines_json) if row.lines_json else [])[:12]:
                lines.append({"sku": l.get("SKU"), "name": l.get("ProductName"),
                              "qty": l.get("Quantity"),
                              "unit_price_aud": _to_float(l.get("UnitPrice"))})
        except (TypeError, ValueError):
            pass
        orders.append({"order_id": row.OrderID, "status": row.OrderStatus,
                       "date": str(row.DatePlaced or ""),
                       "total_aud": _to_float(row.grand_total), "lines": lines})
    return {"matched": bool(orders), "count": len(orders), "orders": orders}


def _top_products(usernames: list[str], limit: int = 10) -> dict[str, Any]:
    """The customer's most-frequently-ordered products, aggregated across
    ALL their orders (by number of orders containing the SKU, then qty).

    Cancelled/Quote orders are excluded to match the order count shown on
    the card (neto_customers uses the same filter), so "frequently
    ordered" is consistent with their lifetime order total.
    """
    if not usernames:
        return {"matched": False, "note": "Unknown caller — no order history."}
    from google.cloud import bigquery
    try:
        limit = max(1, min(25, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    sql = f"""
    SELECT
      JSON_VALUE(line, '$.SKU')         AS sku,
      ANY_VALUE(JSON_VALUE(line, '$.ProductName')) AS name,
      COUNT(DISTINCT o.OrderID)         AS orders_with_sku,
      SUM(SAFE_CAST(JSON_VALUE(line, '$.Quantity') AS INT64)) AS total_qty
    FROM `{PROJECT}.dataform.neto_orders` o,
         UNNEST(JSON_QUERY_ARRAY(o.OrderLine)) AS line
    WHERE o.Username IN UNNEST(@u)
      AND o.OrderStatus NOT IN ('Cancelled', 'Quote')
    GROUP BY sku
    HAVING sku IS NOT NULL AND sku != ''
    ORDER BY orders_with_sku DESC, total_qty DESC
    LIMIT @lim
    """
    job = kb_tools._bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("u", "STRING", usernames),
        bigquery.ScalarQueryParameter("lim", "INT64", limit),
    ]))
    products = [{"sku": r.sku, "name": r.name,
                 "orders_with_sku": r.orders_with_sku, "total_qty": r.total_qty}
                for r in job.result()]
    return {"matched": bool(products), "count": len(products), "products": products}


# ---------------------------------------------------------------------------
# Per-request dispatch builder
# ---------------------------------------------------------------------------

def build_dispatch(*, service, card: dict,
                   can_view_sensitive: bool) -> dict[str, Callable]:
    """Build the name→callable map for one chat request.

    Customer-scoped tools close over ``card`` (already loaded by the
    chat layer). ``get_call_detail`` closes over ``service`` +
    ``can_view_sensitive`` so it can re-fetch and redact. Catalogue
    tools delegate to ``kb_tools``.
    """
    def get_customer_profile() -> dict[str, Any]:
        return _customer_profile(card)

    def get_recent_orders(limit: int = 5) -> dict[str, Any]:
        # Query neto_orders for reliable line items (cached payload omits
        # them for many customers); fall back to the cache on any failure.
        try:
            return _orders_with_lines(card.get("usernames") or [], limit)
        except Exception as exc:
            log.info("c360 get_recent_orders BQ failed, using cache: %s", exc)
            return _recent_orders(card, limit)

    def get_top_products(limit: int = 10) -> dict[str, Any]:
        return _top_products(card.get("usernames") or [], limit)

    def get_calls(limit: int = 8) -> dict[str, Any]:
        return _calls(card, limit)

    def get_related_accounts() -> dict[str, Any]:
        return _related_accounts(card)

    def get_call_detail(session_id: str) -> dict[str, Any]:
        if not session_id:
            return {"error": "session_id required"}
        from app.services.customer_360_service import redact_sensitive_call_details
        details = service.get_call_details(session_id)
        if not can_view_sensitive:
            details = redact_sensitive_call_details(details)
        return details

    usernames = card.get("usernames") or []

    def get_rmas(limit: int = 10) -> dict[str, Any]:
        return _rmas(usernames, limit)

    def get_order_detail(order_id: str) -> dict[str, Any]:
        return _order_detail(usernames, order_id)

    return {
        "get_customer_profile": get_customer_profile,
        "get_recent_orders":    get_recent_orders,
        "get_top_products":     get_top_products,
        "get_order_detail":     get_order_detail,
        "get_rmas":             get_rmas,
        "get_calls":            get_calls,
        "get_related_accounts": get_related_accounts,
        "get_call_detail":      get_call_detail,
        "get_stock_and_price":  kb_tools.get_stock_and_price,
        "list_products":        kb_tools.list_products,
    }


# ---------------------------------------------------------------------------
# Static function declarations passed to Vertex
# ---------------------------------------------------------------------------

def function_declarations():
    from vertexai.generative_models import FunctionDeclaration

    decls = [
        FunctionDeclaration(
            name="get_customer_profile",
            description=(
                "Profile of THE CUSTOMER ON THIS CARD — name, lifetime "
                "orders/value, badge, days since last order, RMA count, "
                "account origin. No arguments; it's always this customer. "
                "Returns matched=False for an unknown caller."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="get_recent_orders",
            description=(
                "This customer's recent orders — date, order ID, status, "
                "total, line items (SKU/qty/name/price). Use for 'what have "
                "they bought', 'what saw do they run', order-status questions."
            ),
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer",
                          "description": "How many recent orders (default 5, max 20)."},
            }},
        ),
        FunctionDeclaration(
            name="get_calls",
            description=(
                "This customer's call history and AI behaviour insights — "
                "lifetime counts (connected/missed/abandoned/voicemail), "
                "typical call types, past problems, the most-recent analysed "
                "call summary, and a list of recent calls each with a "
                "session_id. Use for 'why have they called', call-pattern or "
                "sentiment questions."
            ),
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer",
                          "description": "How many recent calls to list (default 8, max 20)."},
            }},
        ),
        FunctionDeclaration(
            name="get_call_detail",
            description=(
                "Full detail for ONE call — AI summary, transcript, "
                "classifications, sentiment. Pass a session_id from "
                "get_calls. Use only when the agent asks about a specific "
                "call. Sensitive calls are redacted unless the agent is "
                "permitted to view them."
            ),
            parameters={"type": "object", "properties": {
                "session_id": {"type": "string",
                               "description": "session_id from get_calls.recent_calls."},
            }, "required": ["session_id"]},
        ),
        FunctionDeclaration(
            name="get_related_accounts",
            description=(
                "Other Neto accounts linked to this customer — by shared "
                "email, billing address, or phone — plus abandoned guest "
                "stub count. Use for 'do they have another account', "
                "duplicate/household questions."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="get_top_products",
            description=(
                "This customer's MOST-FREQUENTLY-ORDERED products, aggregated "
                "across ALL their orders — each with the number of orders "
                "containing that SKU and total quantity. Use for 'what do "
                "they usually/frequently/regularly order', 'their top "
                "products', 'what do they keep buying'. This is the right "
                "tool for buying-pattern questions — get_recent_orders only "
                "covers the latest few orders."
            ),
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer",
                          "description": "How many top products (default 10, max 25)."},
            }},
        ),
        FunctionDeclaration(
            name="get_rmas",
            description=(
                "This customer's RMAs / returns / refunds — status, dates, "
                "refund amounts (total, subtotal, shipping), internal notes, "
                "and per-item return reason + resolution. Use for ANY refund, "
                "return, RMA, credit, or 'how much did we refund' question. "
                "The refund AMOUNT lives here, not in orders."
            ),
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer",
                          "description": "How many recent RMAs (default 10, max 20)."},
            }},
        ),
        FunctionDeclaration(
            name="get_order_detail",
            description=(
                "Full detail for ONE order by order ID — status, totals "
                "breakdown (grand/subtotal/shipping), ship-to address, "
                "shipping method, and every line item WITH tracking number "
                "and tracking URL. Use for shipping/tracking/'where's my "
                "order'/full-line-detail questions. Get the order ID from "
                "get_recent_orders first if you don't have it."
            ),
            parameters={"type": "object", "properties": {
                "order_id": {"type": "string",
                             "description": "Order ID, e.g. 'JJ626218'."},
            }, "required": ["order_id"]},
        ),
    ]
    # Reuse the catalogue tools verbatim from kb_tools so there's a single
    # source of truth for their schemas.
    catalogue = [d for d in kb_tools.function_declarations()
                 if _decl_name(d) in ("get_stock_and_price", "list_products")]
    return decls + catalogue


def _decl_name(decl) -> str:
    """Extract a FunctionDeclaration's name across SDK versions."""
    name = getattr(decl, "name", None)
    if name:
        return name
    raw = getattr(decl, "_raw_function_declaration", None)
    return getattr(raw, "name", "") if raw else ""
