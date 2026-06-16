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

import logging
from typing import Any, Callable

from app.services import kb_tools

log = logging.getLogger(__name__)


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
        return _recent_orders(card, limit)

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

    return {
        "get_customer_profile": get_customer_profile,
        "get_recent_orders":    get_recent_orders,
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
