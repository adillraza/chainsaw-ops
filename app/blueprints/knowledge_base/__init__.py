"""Knowledge Base blueprint.

Two surfaces:
  * ``/kb``               — agent-facing search page
  * ``/api/kb/search``    — JSON API used by the page (and any future
                            embedded search box on Customer 360)

Both gated by the ``kb.view`` capability.
"""
from flask import Blueprint

knowledge_base_bp = Blueprint("knowledge_base", __name__)

from app.blueprints.knowledge_base import routes  # noqa: E402,F401
