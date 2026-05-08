"""Knowledge Base routes — chat-style RAG plus a JSON retrieval API.

The agent-facing surface is now a multi-turn chat (``/kb``) that streams
Gemini answers grounded in the kb.documents corpus. The pure-retrieval
``/api/kb/search`` endpoint stays around for any future consumer that
just wants ranked products.
"""
from __future__ import annotations

import json
import logging

from flask import Response, jsonify, render_template, request, stream_with_context

from app.auth.abilities import require_capability
from app.blueprints.knowledge_base import knowledge_base_bp
from app.services import kb_chat, kb_service

log = logging.getLogger(__name__)

MAX_TOP_K  = 25
DEFAULT_K  = 8


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@knowledge_base_bp.route("/kb", methods=["GET"])
@require_capability("kb.view")
def index():
    """Render the chat page. The conversation lives in sessionStorage on
    the client; the server just serves the shell."""
    return render_template("knowledge_base/index.html",
                           page_title="Knowledge Base")


# ---------------------------------------------------------------------------
# Chat (RAG, streamed)
# ---------------------------------------------------------------------------

@knowledge_base_bp.route("/api/kb/chat", methods=["POST"])
@require_capability("kb.view")
def chat():
    """Stream a chat turn back to the client as newline-delimited JSON.

    Request body: ``{"messages": [{"role": "user"|"assistant", "content": "..."}, ...]}``
    Response: ``application/x-ndjson`` — one event per line:
      * ``{"type":"sources","hits":[...]}``   — retrieved docs
      * ``{"type":"token","text":"..."}``     — incremental answer chunks
      * ``{"type":"done"}``                   — clean end-of-stream
      * ``{"type":"error","message":"..."}``  — abort
    """
    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages must be a non-empty list"}), 400

    # Defensive: trim down to the fields kb_chat expects, ignore anything
    # else the client decided to round-trip.
    cleaned: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            cleaned.append({"role": role, "content": content})
    if not cleaned or cleaned[-1]["role"] != "user":
        return jsonify({"error": "last message must be from the user"}), 400

    @stream_with_context
    def generate():
        try:
            for event in kb_chat.stream(cleaned):
                yield json.dumps(event, default=str) + "\n"
        except Exception as exc:
            log.warning("kb_chat stream crashed: %s", exc)
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
        },
    )


# ---------------------------------------------------------------------------
# Pure retrieval JSON API (unchanged from Phase 1A — kept for future
# consumers that want ranked products without an answer layer)
# ---------------------------------------------------------------------------

@knowledge_base_bp.route("/api/kb/search", methods=["GET"])
@require_capability("kb.view")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"query": "", "hits": []})
    try:
        top_k = max(1, min(MAX_TOP_K, int(request.args.get("top_k", DEFAULT_K))))
    except (TypeError, ValueError):
        top_k = DEFAULT_K
    try:
        hits = kb_service.search(query, top_k=top_k)
    except Exception as exc:
        return jsonify({"query": query, "hits": [], "error": str(exc)}), 500
    return jsonify({"query": query, "hits": hits})
