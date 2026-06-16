"""Customer 360 chat — an AI assistant scoped to ONE customer.

Reuses the KB chat mechanics (Vertex Gemini Flash + a function-calling
loop + NDJSON streaming) but with three differences:

  1. **Customer pre-seeded.** The chat layer loads the card payload
     (``customer_360_service.get_card``) once, drops a compact CUSTOMER
     CONTEXT block into the system prompt, and binds every customer tool
     to that card via ``c360_tools.build_dispatch`` — the model never
     asks "which customer?" and can't query a different one.
  2. **Warmer prompt.** No catalogue rulebook. The persona is a sharp
     colleague briefing an agent who's about to take (or is on) a call:
     concise, plain-spoken, grounded in the tools.
  3. **Pre-call brief.** ``brief()`` does a single grounded generation
     (no tools needed — the card data is in the prompt) for the panel's
     auto-summary.

Sensitivity is handled in ``c360_tools`` (get_call_detail redaction);
this layer just threads ``can_view_sensitive`` through.

Event protocol matches kb_chat so the frontend reader is reusable:
``token`` / ``tool_call`` / ``tool_result`` / ``done`` / ``error``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

PROJECT  = "chainsawspares-385722"
LOCATION = "us-central1"
MODEL    = "gemini-2.5-flash"   # 2.0-flash-001 was retired by Google (404s); 2.5-flash is the live Flash tier

TEMPERATURE      = 0.3   # a touch warmer than KB (0.2) — conversational, still grounded
# 2.5-flash spends output tokens on (hidden) thinking before the visible
# answer — see the brief above. Budget must cover BOTH or the answer
# truncates mid-sentence. ~1500 leaves ample room for a short reply.
MAX_OUTPUT       = 1500
HISTORY_TURN_CAP = 12

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Customer 360 assistant for chainsawspares.com.au — a sharp, friendly colleague helping a customer-service agent who is about to take, or is already on, a phone call.

You are looking at ONE customer's card with the agent. A CUSTOMER CONTEXT block is given below with the headline facts. For anything deeper — full order lines, call history, a specific call's summary, linked accounts, live stock/price — CALL A TOOL. Never invent orders, prices, stock, dates, or call details.

HOW TO ANSWER:
- Talk like a helpful teammate, not a form. Lead with the answer, in 1–3 short sentences the agent can glance at mid-call. Add a detail or two only if it helps.
- Ground every factual claim (orders, prices, stock, call facts) in CUSTOMER CONTEXT or a tool result. If you don't have it and can't get it from a tool, say so plainly.
- Markdown renders: use **bold** for SKUs/order IDs/key figures and short bullet lists for multiple items. No headings or tables — keep it conversational.
- One concise follow-up offer at the end is fine when it's genuinely useful ("Want their full call history?"), but skip filler like "anything else?".

TOOLS (all scoped to THIS customer unless noted):
- get_customer_profile() — name, lifetime value/orders, badge, recency, RMAs.
- get_recent_orders(limit) — recent orders with line items; use for "what have they bought / what saw do they run / order status".
- get_calls(limit) — call counts, typical reasons, past problems, latest analysed-call summary, and recent calls (each with a session_id).
- get_call_detail(session_id) — one call's AI summary/transcript/sentiment. Only when asked about a specific call.
- get_related_accounts() — other accounts linked by email/address/phone.
- get_stock_and_price(sku) — live Neto online + Ballarat retail stock/price for a SKU (NOT customer-scoped).
- list_products(fits_model, product_type, brand, in_stock_online_only, limit) — catalogue browse for "what fits / what do we have" (NOT customer-scoped).

UNKNOWN CALLER: if the profile is unmatched (no Neto record for this number), say so briefly and still help with product, stock, and fitment questions via the catalogue tools.

Be honest about gaps. A quick "I don't see any prior orders for this number" beats a confident guess."""


def _context_block(card: dict, *, include_call_summary: bool = True) -> str:
    """Compact headline facts about the customer for the system prompt.

    ``include_call_summary=False`` (used by the chat) omits the verbatim
    last-call summary so the model can't just paraphrase it — it must
    call get_calls/get_call_detail to answer call-content questions,
    which keeps answers live, complete, and transparent (tool spinner).
    The brief uses the full version (it's a one-shot with no tools).

    Kept small — the model calls tools for depth. Pulls only what's
    cheap and already in the loaded card payload.
    """
    if not card.get("matched") or not (card.get("customers") or []):
        return ("CUSTOMER CONTEXT\nUnknown caller — no Neto customer matches "
                f"phone {card.get('phone') or '(unknown)'}. No profile or "
                "order history on file for this number.")
    c = (card.get("customers") or [{}])[0]
    name = ((c.get("name_first") or "").strip() + " "
            + (c.get("name_last") or "").strip()).strip() or "(no name on file)"
    h = card.get("call_history") or {}
    b = card.get("call_behavior") or {}
    last = b.get("last_call") or {}
    lines = [
        "CUSTOMER CONTEXT",
        f"Name: {name}",
        f"Phone: {card.get('phone')}",
        f"Email: {c.get('email') or '—'}",
        f"Badge: {c.get('customer_badge') or '—'}",
        f"Lifetime: {c.get('lifetime_order_count') or 0} orders, "
        f"${c.get('lifetime_value') or 0} total, AOV ${c.get('avg_order_value') or 0}",
        f"Last order: {c.get('last_order_date') or '—'} "
        f"({c.get('days_since_last_order')} days ago)" if c.get('last_order_date')
        else "Last order: none",
        f"Lifetime RMAs: {c.get('lifetime_rma_count') or 0}",
        f"Calls: {h.get('total_calls') or 0} lifetime "
        f"(last {h.get('days_since_last_call')}d ago)" if h else "Calls: none on file",
    ]
    if card.get("usernames") and len(card["usernames"]) > 1:
        lines.append(f"Linked accounts: {len(card['usernames'])} share this number")
    if include_call_summary and last.get("summary"):
        lines.append(f"Most recent analysed call ({last.get('call_date') or '?'}, "
                     f"sentiment {last.get('sentiment_label') or '?'}): {last['summary']}")
    elif not include_call_summary:
        lines.append("(For call reasons/history, recent orders, or linked "
                     "accounts, call the matching tool — don't answer from memory.)")
    return "\n".join(lines)


def _build_history(messages: list[dict]):
    from vertexai.generative_models import Content, Part
    prior = messages[:-1][-HISTORY_TURN_CAP:]
    history = []
    for m in prior:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            history.append(Content(role="model", parts=[Part.from_text(content)]))
        elif role == "user":
            history.append(Content(role="user", parts=[Part.from_text(content)]))
    return history


def _new_model(system_instruction: str, with_tools: bool):
    """Build a GenerativeModel. Not cached — the system instruction
    carries per-customer context, so each request needs its own."""
    import vertexai
    from vertexai.generative_models import GenerativeModel, Tool
    from app.services import c360_tools
    vertexai.init(project=PROJECT, location=LOCATION)
    kwargs: dict[str, Any] = {"system_instruction": system_instruction}
    if with_tools:
        kwargs["tools"] = [Tool(function_declarations=c360_tools.function_declarations())]
    return GenerativeModel(MODEL, **kwargs)


FOLLOWUP_INSTRUCTION = """You propose the NEXT questions a customer-service agent might ask about THIS customer, given the context and conversation so far.

Output ONLY a JSON array of 2–3 short questions (each ≤ 8 words), specific to this customer and what's just been discussed — not generic. Phrase them as the agent would ask the assistant. No prose, no numbering, no markdown. If the conversation already covered something, suggest a sensible next step instead of repeating it.

Good examples (shape, not content): ["What was the refund for?", "Has it been paid back yet?", "What's their most-ordered part?"]"""


def _suggest_followups(context_block: str, convo: str) -> list[str]:
    """Best-effort 2–3 short follow-up questions for the chip row.

    Uses a cheap structured-JSON generation (array of strings) so parsing
    is robust. Returns [] on any failure — suggestions are a nicety and
    must never break the turn.
    """
    try:
        from vertexai.generative_models import GenerationConfig
        model = _new_model(
            FOLLOWUP_INSTRUCTION + "\n\n" + context_block
            + "\n\nCONVERSATION SO FAR:\n" + (convo or "(none yet)"),
            with_tools=False)
        cfg = GenerationConfig(
            temperature=0.4, max_output_tokens=800,
            response_mime_type="application/json",
            response_schema={"type": "array", "items": {"type": "string"}},
        )
        resp = model.generate_content(
            "List the follow-up questions.", generation_config=cfg)
        items = json.loads((getattr(resp, "text", None) or "[]"))
        if isinstance(items, list):
            return [str(x).strip() for x in items if str(x).strip()][:3]
    except Exception as exc:
        log.info("c360_chat: followups failed: %s", exc)
    return []


def stream(phone: str, messages: list[dict], *,
           can_view_sensitive: bool = False) -> Iterator[dict[str, Any]]:
    """Yield events for one chat turn, scoped to the customer at ``phone``."""
    if not messages or messages[-1].get("role") != "user":
        yield {"type": "error", "message": "last message must be from user"}
        return
    last_user = (messages[-1].get("content") or "").strip()
    if not last_user:
        yield {"type": "error", "message": "empty question"}
        return

    t_total = time.perf_counter()

    try:
        from app.services.customer_360_service import customer_360_service
        from app.services import c360_tools
        from vertexai.generative_models import GenerationConfig, Part

        card = customer_360_service.get_card(phone)
        dispatch = c360_tools.build_dispatch(
            service=customer_360_service, card=card,
            can_view_sensitive=can_view_sensitive)

        lean_ctx = _context_block(card, include_call_summary=False)
        system_prompt = SYSTEM_PROMPT + "\n\n" + lean_ctx
        chat = _new_model(system_prompt, with_tools=True).start_chat(
            history=_build_history(messages), response_validation=False)
        gen_cfg = GenerationConfig(temperature=TEMPERATURE, max_output_tokens=MAX_OUTPUT)

        message_to_send: Any = last_user
        max_tool_loops = 4
        t_first = None
        full_text = ""
        for loop in range(max_tool_loops + 1):
            response = chat.send_message(message_to_send, generation_config=gen_cfg)
            cand = response.candidates[0] if response.candidates else None
            parts = (cand.content.parts if cand and cand.content else []) or []

            fcalls, text_pieces = [], []
            for p in parts:
                if getattr(p, "function_call", None) and p.function_call.name:
                    fcalls.append(p.function_call)
                else:
                    txt = getattr(p, "text", None)
                    if txt:
                        text_pieces.append(txt)

            if fcalls:
                if loop >= max_tool_loops:
                    log.warning("c360_chat: tool loop cap hit (%d) — bailing", loop)
                    break
                fn_responses = []
                for fc in fcalls:
                    args = dict(fc.args) if fc.args else {}
                    yield {"type": "tool_call", "name": fc.name, "args": args}
                    try:
                        fn = dispatch.get(fc.name)
                        result = fn(**args) if fn else {"error": f"unknown tool {fc.name}"}
                    except Exception as exc:
                        log.warning("c360_chat: tool %s threw: %s", fc.name, exc)
                        result = {"error": str(exc)}
                    yield {"type": "tool_result", "name": fc.name, "result": result}
                    fn_responses.append(Part.from_function_response(
                        name=fc.name, response={"result": result}))
                message_to_send = fn_responses
                continue

            full_text = "".join(text_pieces)
            t_first = time.perf_counter()
            for i in range(0, len(full_text), 30):
                yield {"type": "token", "text": full_text[i:i + 30]}
            break

        # Conversation-aware follow-up chips (best-effort, after the answer
        # so it never delays the reply the agent is reading).
        if full_text:
            convo_lines = [f"{m.get('role')}: {m.get('content')}"
                           for m in messages[-6:] if m.get("content")]
            convo_lines.append(f"assistant: {full_text}")
            items = _suggest_followups(lean_ctx, "\n".join(convo_lines))
            if items:
                yield {"type": "suggestions", "items": items}
    except Exception as exc:
        log.warning("c360_chat: generation failed: %s", exc)
        yield {"type": "error", "message": f"generation failed: {exc}"}
        return

    elapsed_ms = (time.perf_counter() - t_total) * 1000
    log.info("c360.chat phone=%s total=%.0fms q=%r",
             phone, elapsed_ms, last_user[:80])
    yield {"type": "done"}


BRIEF_INSTRUCTION = """You are the Customer 360 assistant. Write a TIGHT pre-call brief for the agent about to take this customer's call — what they'd want to know in the first five seconds.

Rules:
- 2–4 short sentences OR up to 4 terse bullets. No preamble, no headings, no sign-off.
- Lead with who they are and their value to us (badge / lifetime / recency). Then the single most useful recent signal: last order + status, or why they've been calling, or an unresolved problem.
- Only state facts present in CUSTOMER CONTEXT below. Don't invent or pad. If there's little to go on, a one-liner is correct.
- For an unknown caller, say so in one line — that's the whole brief.
- Plain, calm tone. **bold** for the name and any order ID/figure. No "anything else?" filler."""


def brief(phone: str, *, can_view_sensitive: bool = False) -> Iterator[dict[str, Any]]:
    """Stream an auto-generated pre-call brief from the loaded card data.

    No tools — the headline facts in the context block are enough, which
    keeps it to a single fast generation. ``can_view_sensitive`` is
    accepted for symmetry/future use; the brief only ever uses the same
    aggregate behaviour insights the card already shows this viewer.
    """
    try:
        from app.services.customer_360_service import customer_360_service
        from vertexai.generative_models import GenerationConfig

        card = customer_360_service.get_card(phone)
        system_prompt = BRIEF_INSTRUCTION + "\n\n" + _context_block(card)
        model = _new_model(system_prompt, with_tools=False)
        # 1200 covers 2.5-flash's thinking tokens + the short brief itself.
        gen_cfg = GenerationConfig(temperature=0.2, max_output_tokens=1200)
        resp = model.generate_content(
            "Write the pre-call brief.", generation_config=gen_cfg)
        text = (getattr(resp, "text", None) or "").strip()
        for i in range(0, len(text), 30):
            yield {"type": "token", "text": text[i:i + 30]}

        # Customer-aware starter chips, seeded from the brief itself so the
        # very first suggestions already reference this customer's situation.
        ctx = _context_block(card)
        items = _suggest_followups(ctx, "assistant (pre-call brief): " + text)
        if items:
            yield {"type": "suggestions", "items": items}
    except Exception as exc:
        log.warning("c360_chat: brief failed: %s", exc)
        yield {"type": "error", "message": f"brief failed: {exc}"}
        return
    yield {"type": "done"}
