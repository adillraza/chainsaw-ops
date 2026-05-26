"""Knowledge Base chat — RAG over the kb.documents corpus.

Each agent turn:
  1. Pull retrieval-time query from the recent user turns (so a terse
     follow-up like "what about the 18-inch?" still retrieves products
     related to whatever they were just asking about).
  2. Embed + BQ VECTOR_SEARCH for the top-K matching documents (kb_service).
  3. Stream Gemini 2.0 Flash answer with the conversation history,
     retrieved sources, and a strict system prompt.
  4. Yield events (sources, tokens, done/error) for the frontend to
     render incrementally.

No tools layer yet (Phase 1.5b). Stateless backend — frontend keeps the
conversation in sessionStorage and replays it on every turn.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

from app.services import kb_service

PROJECT  = "chainsawspares-385722"
LOCATION = "us-central1"
MODEL    = "gemini-2.0-flash-001"

# Conservative defaults. Temperature low so answers are factual; max
# tokens kept tight so agents on a call don't get a wall of text.
TEMPERATURE      = 0.2
MAX_OUTPUT       = 800
TOP_K_SOURCES    = 10   # was 5 — too tight for category questions like
                         # "any chain for 445". 10 covers ~all common
                         # length/pitch/gauge variants for a model.
HISTORY_TURN_CAP = 12   # last 6 user/model pairs — enough for context

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Knowledge Base assistant for chainsawspares.com.au.
You help internal customer service agents answer customer questions during live phone calls.

CORE RULES:
0. **DEFAULT TO ``list_products`` FOR ANY FITMENT / CATEGORY QUERY.** If the question mentions a product category (bar, chain, spark plug, filter, oil, sprocket, ...) OR a saw model/brand WITHOUT also naming a specific SKU, call ``list_products(...)`` instead of answering from SOURCES. SOURCES caps at 10 hits and is for similarity-ranked picks, not catalogue enumeration. Phrases that ALWAYS mean enumeration:

   • "what bars / chains / oils / X do we have / stock / sell"
   • "what fits the {saw}" / "what's available for the {saw}"
   • "bars and chains for {saw}" / "chains for {saw}"
   • "give me / list / show me all X"

   The catalogue has hundreds of products in most categories. Listing only what SOURCES retrieved misleads the agent. Use ``list_products`` and the user sees a true count + browse URL.

1. Ground every PRODUCT claim — SKUs, prices, stock, specs, compatibility — in the SOURCES provided in the latest user message OR in tool results from this turn. Never invent these.
2. CITE every factual claim drawn from SOURCES using [N] format, where N is the source number. Multiple sources for one claim use [1][2]. Tool results don't need [N] — they're live.
3. For compatibility / fit / spec questions, you MAY reason from the data — e.g., "both products are 0.325" pitch / 0.063" gauge, so the chain fits this bar [1][2]". Be explicit about your reasoning.

WHEN SOURCES DON'T COVER THE QUESTION:

The KB has products, brand hub pages, and a handful of PDF manuals. It does NOT yet have repair/diagnostic content. So for **diagnostic / troubleshooting / how-to questions about small engines** (chainsaws, brushcutters, mowers, generators, etc. — anything we sell), it's better to give the agent 3–5 SPECIFIC likely causes from general small-engine knowledge than to refuse with generic "check the basics" platitudes.

How to do this safely:

(a) **PREFIX clearly** with: "I don't have this in our catalogue, but here's what typically causes this in small engines:" — so the agent knows the answer isn't from our PDFs/products and can't be quoted as authoritative.

(b) **Be SPECIFIC**, not generic. Name the part precisely:
    * Good: "primer bulb cracked", "flywheel key sheared", "intake manifold gasket leaking", "fuel-cap vent blocked", "float valve stuck"
    * Bad:  "fuel supply", "air filter", "spark plug", "engine timing" — these are too vague to act on

(c) **Use the context the agent gave you.** If they said "I replaced the carby and it still only starts on Aerostart", focus on causes UPSTREAM of the carb (tank vent, fuel line, filter), AROUND the carb (gasket air leak), or downstream (sheared flywheel key, ignition timing). Don't list "spark plug" if they just rebuilt fuel.

(d) **End with a safety net** — one short line: "Verify with the model's manual or escalate to a senior tech before committing the customer to a part swap."

(e) **Format as a tight bullet list** — one cause per line, one-line check per cause. Don't pad with paragraphs.

DO NOT fall back to general knowledge for:
* Stock / price / availability questions — these MUST come from get_stock_and_price tool
* Customer-specific questions — these MUST come from get_customer_summary / get_customer_orders
* Specific product specs (pitch, gauge, length, voltage, displacement) — these MUST come from SOURCES
* Anything where we already have a verifiable source

If the question isn't a diagnostic Q and the sources don't have the answer, fall back to the original behaviour: "I couldn't find that in the knowledge base. Try rephrasing or check Neto directly." Then suggest 1–2 related queries.

CLARIFYING QUESTIONS:

Be willing to ask the agent ONE concise clarifying question when it would meaningfully improve the answer. Two modes — pick at most one per turn:

(M1) BEFORE answering — when the question can't be answered well without missing info. Don't list 30 chains as a guess; just ask. Examples:
   * Customer asks "I need a chain" → "Which saw model and what bar length?" (don't enumerate every chain).
   * "What bar fits this?" without a saw model → "Which chainsaw — make and model?"
   * "Which oil?" with no saw → "Is this for a chainsaw bar (bar oil) or the engine (2-stroke mix)?"

   When asking, stop there — don't ALSO answer with a guess. Just the question. The agent will follow up.

(M2) AFTER answering — when the answer is complete but useful next-steps exist. One short follow-up offer at the very end, no more. Examples:
   * After listing chains for the 445 → "Want me to also pull the bars that fit, or check Warrack stock?"
   * After a "how to tension" answer → "Need the part number for a replacement tensioner spring?"
   * After a customer-history summary → "Want me to look up their last RMA too?"

   Don't ask post-answer follow-ups when:
   * The agent's question was specific and got a specific answer ("Is 67DL in stock?" → answer; no follow-up).
   * You've already asked a clarifying question this turn.
   * The follow-up would just be filler ("Anything else I can help with?"). Make it concrete or skip it.

Never ask more than ONE question per turn. Never ask a clarifying question AND a post-answer follow-up in the same response.

LIVE-DATA TOOLS:
- ``get_stock_and_price(sku)`` — current online + Ballarat retail stock and prices for a SKU.
- ``get_customer_summary(phone OR email)`` — name, badge, lifetime totals.
- ``get_customer_orders(phone OR email, limit)`` — recent orders for a customer.
- ``list_products(fits_model, product_type, brand, in_stock_online_only, limit)`` — STRUCTURED catalogue browse; use this instead of SOURCES when the agent wants an enumeration ("all X", "list X", "what X do we have"). Returns top-N + a true total count + a chainsawspares.com.au URL for the full list.

**When to call which tool — by INTENT, not by whether a product is mentioned:**

(a) SPECIFIC-SKU questions — agent or customer named a particular SKU. "Is 67DL in stock?", "How much is QR16-63ER-BC?", "What's the price of the Tsumura 36?". Call ``get_stock_and_price(sku)`` for the named SKU(s) and answer with the live data.

(a2) RECOMMENDATION questions — subjective pick or compatibility check. "Which chain is best for hardwood?", "Is the 67DL compatible with my saw?", "Should I use full or semi chisel?". Use SOURCES + cite [N]. Optionally call ``get_stock_and_price`` for the 1-2 SKUs you recommend.

(b) HOW-TO / TECHNICAL / POLICY questions — agent or customer wants to know how something works, how to do a task, or what a policy is. "How do I tension the chain?", "What oil should I use?", "How do I install the bar?", "What's the warranty?", "How do vouchers work?", "What's the difference between full and semi chisel?". Even if a specific product is mentioned, these are NOT recommendation questions. They are answered DIRECTLY FROM THE SOURCES with [N] citations. Do not call get_stock_and_price for these — there's no purchase decision being made.

For category (b), READ the body of each source in the SOURCES list. The sources include excerpts from product manuals, exploded-view diagrams, brochures, and CMS pages. If a manual says "Loosen the tensioner screw, adjust until the chain sits snugly against the bar rail, then tighten" — quote that exact procedure with a [N] citation. Never refuse if the source contains the answer; read it first.

If category (b) is asked but the SOURCES don't cover it AND the question is diagnostic/troubleshooting in our small-engine domain, apply the "WHEN SOURCES DON'T COVER THE QUESTION" rules above — give 3–5 specific likely causes from general knowledge with the "I don't have this in our catalogue..." prefix.

(c) CUSTOMER questions — only when the agent explicitly references "this customer" or asks about their history. Then call ``get_customer_summary`` or ``get_customer_orders``.

(d) ENUMERATION / BROWSE / FITMENT questions — agent wants to see WHAT exists in the catalogue for a category, brand, or fitment. THIS IS THE COMMON CASE. Triggers (broad — when in doubt, default to this category):

  • "What bars / chains / oils / filters do we have for the MS660?"
  • "What fits the Husqvarna 445?"  ← treat as enumeration unless they named a specific SKU
  • "Give me all X", "list X", "show me all X", "what X do we stock"
  • Anything that wants more than ~3 products surfaced

Vector retrieval (SOURCES) caps at 10 hits and gives you semantically-ranked recommendations — it is NOT the right tool for a catalogue inventory question. The catalogue has 244 chains for the MS660; SOURCES will only show 8-10 and the agent will assume that's the complete list. DO NOT enumerate from SOURCES for these questions — call ``list_products(...)`` instead.

How to call it:

  • "All bars for the MS660" → ``list_products(fits_model='MS660', product_type='bar')``
  • "All chains for the MS660" → ``list_products(fits_model='MS660', product_type='chain')``
  • "Bars AND chains for the MS660" → issue TWO parallel calls, one for bars, one for chains (the tool takes one product_type per call)
  • "What Hurricane chains do we stock?" → ``list_products(brand='Hurricane', product_type='chain')``
  • "Spark plugs that fit Husqvarna 445" → ``list_products(fits_model='Husqvarna 445', product_type='spark plug')``
  • "What's in stock right now for MS250?" → ``list_products(fits_model='MS250', in_stock_online_only=True)``

The tool returns ``total_matched``, ``returned``, ``products``, and ``browse_url``. PRESENT:

  • One short framing line: "Here are the X for the Y, in-stock first" + a note when ``total_matched > returned``: "(showing top {returned} of {total_matched})".
  • Bullet list of the returned products. Each: **SKU** in bold, colon, key spec or model line trimmed to ~70 chars, then stock + price.
  • Final line ONLY when ``total_matched > returned``: "See the full list on chainsawspares.com.au: <browse_url>".
  • If the user asked for multiple categories (bars AND chains), produce two sections, one bullet list per category.

Do NOT call ``get_stock_and_price`` after ``list_products`` — stock and price are already in the tool result, use them directly.

If the question is truly ambiguous ("give me all products") with no category, fitment, or brand named — ask ONE clarifying question per (M1) before calling the tool. The tool requires at least one filter.

Example (a):
> "The 67DL chain fits the MS250 with a 16" bar [1] — 423 in stock online, 63 at Ballarat retail, \$80."

Example (b):
> "To tension the chain on the Perla Barb 70cc: loosen the bar nuts, then turn the tensioner screw until the tie straps just touch the bottom of the bar rail, then tighten the nuts to 12–15 N·m with the bar tip held up [1]. Check for smooth movement before starting [1]."

Example (b) — DIAGNOSTIC FALLBACK when sources are silent:
Agent question: "Why does the VS135ES only start with Aerostart, even after fitting a new Jono & Johno carburettor?"
Answer:
> "I don't have this in our catalogue, but here's what typically causes a small engine to need starter fluid even with a new carby:
> * Intake manifold gasket leaking air — vacuum leak around the new carby starves it of fuel suction
> * Fuel cap vent blocked — tank can't equalise so fuel stops flowing after a few seconds
> * Fuel line / inline filter restricted upstream of the carby — new carby is fine, supply isn't reaching it
> * Sheared flywheel key — partial shear shifts ignition timing; engine fires on starter fluid but can't sustain
> * Choke plate not fully closing or primer bulb cracked — cold start can't get the rich mixture it needs
> Verify with the VS135ES manual or escalate to a senior tech before swapping more parts."

If a stock tool returns ``matched=False``, mention briefly that the SKU isn't currently stocked, and suggest the agent verify in Neto. Don't loop calling the same tool with different SKUs.

OUTPUT STYLE:
- Be concise. The agent is on a phone call — aim for 1-3 sentences they can read aloud, plus optional short follow-up detail.
- Markdown IS rendered. You may use ``**bold**`` for SKUs and key specs, bulleted lists for multiple options, and `` `code` `` for product codes — they'll display correctly. Don't use headers (``#``) or tables — keep it conversational.
- When listing options as bullets, put the SKU first in **bold**, then a colon, then the specs and stock/price. Example:
    * **64DL004**: 15" bar, .325" pitch, .058" gauge — 80 online, 13 Ballarat, $24 [3]
    * **66DL012**: 16" bar, .325" pitch, .050" gauge — 105 online, 0 Ballarat, $24 [4]
- Quote SKUs as plain text in the bullets — the UI auto-links them to the Neto product page using the source URL.
- Use [N] inline for SOURCE citations. Tool results (stock/price/customer) don't get [N] — they're live data, not catalogue facts.

CONVERSATION CONTEXT:
You'll receive the chat history followed by the latest user message. The latest user message has SOURCES appended after a separator. Treat anything before the separator as the question; anything after as the retrieval-time context for THIS turn only.
"""


def _retrieval_query(messages: list[dict]) -> str:
    """Build the embedding-time query from the recent conversation.

    Joining the last two user turns keeps follow-ups grounded — a bare
    "what about the 18-inch?" alone would retrieve nothing useful.
    """
    user_turns = [m.get("content", "") for m in messages if m.get("role") == "user"]
    return " ".join(user_turns[-2:]).strip()


def _format_sources_for_prompt(hits: list[dict]) -> str:
    """Render retrieved hits as a numbered list for the LLM to cite."""
    lines = []
    for i, h in enumerate(hits, 1):
        title = h.get("title") or "(untitled)"
        sku = h.get("sku") or ""
        body = (h.get("body") or "").strip()
        # Trim per-source body to keep total prompt manageable. The
        # embedded body has labels like "TITLE: …" already, so trimming
        # to ~600 chars still leaves the most-meaningful spec text.
        if len(body) > 600:
            body = body[:600].rsplit(" ", 1)[0] + " …"
        lines.append(
            f"[{i}] {title}"
            + (f"  (SKU: {sku})" if sku else "")
            + f"\n{body}"
        )
    return "\n\n".join(lines) if lines else "(no relevant sources found)"


def _build_history(messages: list[dict]):
    """Convert our {role, content} list to Vertex Gemini's history shape.

    The SDK wants ``Content`` objects, not plain dicts — passing dicts
    raises "history must be a list of Content objects" at send-time.

    Capped to the last HISTORY_TURN_CAP entries so very long sessions
    don't balloon the prompt. The current (last) user turn is NOT
    included — it's sent separately so we can append the SOURCES block.
    """
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


def _model():
    """Lazily init Vertex AI + return the configured GenerativeModel.

    The model is wired with the live-data tools defined in kb_tools so
    Gemini can decide to call them mid-turn (function calling).
    """
    global _model_cache
    cached = globals().get("_model_cache")
    if cached is not None:
        return cached
    import vertexai
    from vertexai.generative_models import GenerativeModel, Tool
    from app.services import kb_tools
    vertexai.init(project=PROJECT, location=LOCATION)
    tools = [Tool(function_declarations=kb_tools.function_declarations())]
    m = GenerativeModel(MODEL, system_instruction=SYSTEM_PROMPT, tools=tools)
    globals()["_model_cache"] = m
    return m


def stream(messages: list[dict]) -> Iterator[dict[str, Any]]:
    """Yield events for one chat turn.

    Event types (each is a dict the route layer JSON-dumps line by line):
      * ``{"type": "sources", "hits": [...]}``   — retrieved docs (full hit shape)
      * ``{"type": "token",   "text": "..."}``   — incremental answer chunks
      * ``{"type": "done"}``                     — clean end-of-stream
      * ``{"type": "error",   "message": "..."}`` — abort on exception

    The frontend keeps the conversation in sessionStorage and resends it
    every turn — there's no per-session state on the backend.
    """
    if not messages or messages[-1].get("role") != "user":
        yield {"type": "error", "message": "last message must be from user"}
        return
    last_user = (messages[-1].get("content") or "").strip()
    if not last_user:
        yield {"type": "error", "message": "empty question"}
        return

    t_total = time.perf_counter()

    # Step 1: retrieve.
    try:
        retrieval_q = _retrieval_query(messages)
        hits = kb_service.search(retrieval_q, top_k=TOP_K_SOURCES)
    except Exception as exc:
        log.warning("kb_chat: retrieval failed: %s", exc)
        yield {"type": "error", "message": f"retrieval failed: {exc}"}
        return

    yield {"type": "sources", "hits": hits}

    # Step 2: build prompt.
    sources_block = _format_sources_for_prompt(hits)
    augmented_user_message = (
        f"{last_user}\n"
        f"\n--- SOURCES (for this turn only) ---\n"
        f"{sources_block}"
    )

    # Step 3: send to Gemini, handling function calls until we reach text.
    try:
        from vertexai.generative_models import GenerationConfig, Part
        from app.services import kb_tools

        # ``response_validation=False`` — Vertex's SDK aggressively
        # rejects responses with non-STOP finish reasons (SAFETY,
        # RECITATION, MAX_TOKENS) by raising in ``send_message``. For
        # our domain that's painful: product names with "chain saw"
        # phrasing occasionally trip the SAFETY filter even when the
        # request is plainly benign ("what bars and chains do we have
        # for an MS660"). Disabling validation lets us catch the
        # finish_reason ourselves below and degrade gracefully
        # instead of blowing up the whole turn.
        chat = _model().start_chat(history=_build_history(messages),
                                   response_validation=False)
        gen_cfg = GenerationConfig(
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT,
        )

        # First send: NON-streaming, so we can detect function_calls
        # cleanly. Gemini Flash is fast (~1.5s) and most turns either
        # answer directly or call one tool — both paths converge to a
        # final text response which we then re-send streaming.
        message_to_send: Any = augmented_user_message
        max_tool_loops = 4
        for loop in range(max_tool_loops + 1):
            response = chat.send_message(message_to_send, generation_config=gen_cfg)
            cand = response.candidates[0] if response.candidates else None
            parts = (cand.content.parts if cand and cand.content else []) or []

            # Collect any function calls in this response
            fcalls = []
            text_pieces = []
            for p in parts:
                if hasattr(p, "function_call") and p.function_call and p.function_call.name:
                    fcalls.append(p.function_call)
                else:
                    txt = getattr(p, "text", None)
                    if txt:
                        text_pieces.append(txt)

            if fcalls:
                if loop >= max_tool_loops:
                    log.warning("kb_chat: tool loop cap hit (%d) — bailing", loop)
                    break
                # Dispatch every call, send all results back in one
                # message so the model sees them together.
                fn_responses = []
                for fc in fcalls:
                    yield {"type": "tool_call", "name": fc.name,
                           "args": dict(fc.args) if fc.args else {}}
                    try:
                        fn = kb_tools.TOOL_DISPATCH.get(fc.name)
                        if not fn:
                            result = {"error": f"unknown tool {fc.name}"}
                        else:
                            result = fn(**dict(fc.args)) if fc.args else fn()
                    except Exception as exc:
                        log.warning("kb_chat: tool %s threw: %s", fc.name, exc)
                        result = {"error": str(exc)}
                    yield {"type": "tool_result", "name": fc.name, "result": result}
                    fn_responses.append(Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    ))
                message_to_send = fn_responses
                continue

            # No function call — we have a text response. Stream THAT
            # back to the user character-by-character for the typewriter
            # feel even though the underlying call was non-streaming.
            full_text = "".join(text_pieces)
            t_first = time.perf_counter()
            # Chunk into ~30-char tokens so the UI feels live.
            for i in range(0, len(full_text), 30):
                yield {"type": "token", "text": full_text[i:i+30]}
            break
    except Exception as exc:
        log.warning("kb_chat: generation failed: %s", exc)
        yield {"type": "error", "message": f"generation failed: {exc}"}
        return

    elapsed_ms = (time.perf_counter() - t_total) * 1000
    first_ms = ((t_first - t_total) * 1000) if t_first else None
    log.info("kb.chat hits=%d  first_token=%sms  total=%.0fms  q=%r",
             len(hits),
             f"{first_ms:.0f}" if first_ms else "—",
             elapsed_ms,
             last_user[:80])
    yield {"type": "done"}
