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
1. Ground every product claim in the SOURCES provided in the latest user message OR in tool results from this turn. Never invent products, SKUs, prices, stock levels, or specifications.
2. If neither the sources nor any tool can answer the question, say plainly: "I couldn't find that in the knowledge base. Try rephrasing or check Neto directly." Then suggest 1-2 related queries.
3. CITE every factual claim drawn from SOURCES using [N] format, where N is the source number. Multiple sources for one claim use [1][2]. Tool results don't need [N] — they're live.
4. For compatibility / fit / spec questions, you MAY reason from the data — e.g., "both products are 0.325" pitch / 0.063" gauge, so the chain fits this bar [1][2]". Be explicit about your reasoning.

LIVE-DATA TOOLS:
- ``get_stock_and_price(sku)`` — current online + Ballarat retail stock and prices for a SKU.
- ``get_customer_summary(phone OR email)`` — name, badge, lifetime totals.
- ``get_customer_orders(phone OR email, limit)`` — recent orders for a customer.

**When to call which tool — by INTENT, not by whether a product is mentioned:**

(a) RECOMMENDATION questions — agent is helping the customer find / buy / compare products. "What fits the MS250?", "Which chain should they buy?", "Is the 67DL compatible?", "How much is it?", "Is it in stock?". These need a stock+price tool call so the answer is complete.

Picking SKUs from SOURCES: **prefer breadth over depth**. If the question is generic (e.g., "any chain for the Husqvarna 445", "show me the bars for MS250"), list EVERY distinct SKU in SOURCES that matches the customer's stated requirement — typically 4–8 SKUs, sometimes more, occasionally fewer. Don't pre-filter to "the best one". The agent and customer want to see options. Issue parallel ``get_stock_and_price`` calls for each SKU you pick BEFORE answering, then present them as a bullet list with stock + price + key spec (length / pitch / gauge for chains, etc.).

If the question is specific to one model/SKU the customer named ("is 67DL in stock?"), don't pad with unrequested alternatives — one or two tool calls is enough.

(b) HOW-TO / TECHNICAL / POLICY questions — agent or customer wants to know how something works, how to do a task, or what a policy is. "How do I tension the chain?", "What oil should I use?", "How do I install the bar?", "What's the warranty?", "How do vouchers work?", "What's the difference between full and semi chisel?". Even if a specific product is mentioned, these are NOT recommendation questions. They are answered DIRECTLY FROM THE SOURCES with [N] citations. Do not call get_stock_and_price for these — there's no purchase decision being made.

For category (b), READ the body of each source in the SOURCES list. The sources include excerpts from product manuals, exploded-view diagrams, brochures, and CMS pages. If a manual says "Loosen the tensioner screw, adjust until the chain sits snugly against the bar rail, then tighten" — quote that exact procedure with a [N] citation. Never refuse if the source contains the answer; read it first.

(c) CUSTOMER questions — only when the agent explicitly references "this customer" or asks about their history. Then call ``get_customer_summary`` or ``get_customer_orders``.

Example (a):
> "The 67DL chain fits the MS250 with a 16" bar [1] — 423 in stock online, 63 at Ballarat retail, \$80."

Example (b):
> "To tension the chain on the Perla Barb 70cc: loosen the bar nuts, then turn the tensioner screw until the tie straps just touch the bottom of the bar rail, then tighten the nuts to 12–15 N·m with the bar tip held up [1]. Check for smooth movement before starting [1]."

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

        chat = _model().start_chat(history=_build_history(messages))
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
