"""Knowledge Base routes — agent search page + JSON API."""
from __future__ import annotations

from flask import jsonify, render_template, request

from app.auth.abilities import require_capability
from app.blueprints.knowledge_base import knowledge_base_bp
from app.services import kb_service


MAX_TOP_K  = 25
DEFAULT_K  = 8


@knowledge_base_bp.route("/kb", methods=["GET"])
@require_capability("kb.view")
def index():
    """Render the search page. Server-side searches when ``q`` is given so
    deep-linkable URLs (``/kb?q=stihl+ms250+chain``) work + are shareable.
    HTMX swaps the results section on subsequent typing so the page
    doesn't fully reload."""
    query = (request.args.get("q") or "").strip()
    hits: list[dict] = []
    if query:
        try:
            hits = kb_service.search(query, top_k=DEFAULT_K)
        except Exception as exc:
            # Don't 500 a search box — render the page with an error state.
            return render_template("knowledge_base/index.html",
                                   query=query, hits=[], error=str(exc),
                                   page_title="Knowledge Base")
    return render_template("knowledge_base/index.html",
                           query=query, hits=hits, error=None,
                           page_title="Knowledge Base")


@knowledge_base_bp.route("/kb/search", methods=["GET"])
@require_capability("kb.view")
def search_partial():
    """HTMX endpoint — returns the result-list HTML fragment. Hooked up
    to the search input so typing yields live results without a full
    page reload."""
    query = (request.args.get("q") or "").strip()
    hits: list[dict] = []
    error: str | None = None
    if query:
        try:
            hits = kb_service.search(query, top_k=DEFAULT_K)
        except Exception as exc:
            error = str(exc)
    return render_template("knowledge_base/_results.html",
                           query=query, hits=hits, error=error)


@knowledge_base_bp.route("/api/kb/search", methods=["GET"])
@require_capability("kb.view")
def api_search():
    """JSON API. Future embedded search box on Customer 360 (or any
    other consumer) calls this directly."""
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
