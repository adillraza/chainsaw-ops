"""Ability lookup + route decorator + template helper.

Design:

* A process-wide cache maps ``role_name -> frozenset[capability]`` so
  ``user.can(cap)`` is a dict-lookup on the hot path. The cache is
  invalidated whenever a role is created/updated/deleted (see
  :func:`invalidate_cache`).
* ``user.can(cap)`` returns ``False`` for anonymous users -- anything that
  requires login must still be wrapped in ``@login_required``.
* ``@require_capability(cap)`` short-circuits with a 403 (HTML flash +
  redirect for non-HTMX requests, bare 403 for HTMX so fragment swaps fail
  cleanly).

This module deliberately has **no imports from the ``app.models`` package
at module top level** to avoid circular imports during app factory setup;
the ``Role`` model is imported lazily inside the cache loader.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user


# ---------------------------------------------------------------------------
# In-process role -> capability cache
# ---------------------------------------------------------------------------

_ROLE_CAP_CACHE: dict[str, frozenset[str]] | None = None


def _load_cache() -> dict[str, frozenset[str]]:
    """Load every role's capability set from the DB into a flat dict."""
    from app.models.role import Role

    cache: dict[str, frozenset[str]] = {}
    for role in Role.query.all():
        cache[role.name] = frozenset(role.capabilities or [])
    return cache


def _get_cache() -> dict[str, frozenset[str]]:
    global _ROLE_CAP_CACHE
    if _ROLE_CAP_CACHE is None:
        _ROLE_CAP_CACHE = _load_cache()
    return _ROLE_CAP_CACHE


def invalidate_cache() -> None:
    """Drop the cached role->capability map.

    Call this after any write that changes a role's capability list (create,
    update, delete). The next ``user.can(...)`` will refresh from the DB.
    """
    global _ROLE_CAP_CACHE
    _ROLE_CAP_CACHE = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def user_can(user, capability: str) -> bool:
    """Return True if ``user`` has ``capability`` via their current role."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    role_name = getattr(user, "role", None)
    if not role_name:
        return False
    return capability in _get_cache().get(role_name, frozenset())


def capabilities_for(role_name: str) -> frozenset[str]:
    """Return the capability set for ``role_name`` (empty if unknown)."""
    return _get_cache().get(role_name, frozenset())


def require_capability(capability: str) -> Callable:
    """Route decorator that enforces ``user_can(current_user, capability)``.

    Usage::

        @purchase_orders_bp.route("/…")
        @login_required
        @require_capability("reviews.flag")
        def comparison_row_flag(...):
            ...

    For HTMX requests we return a bare 403 so the caller's fragment swap
    fails loudly; for normal HTTP we redirect to the dashboard with a flash
    message so the user isn't dropped on a blank page.
    """

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not user_can(current_user, capability):
                if request.headers.get("HX-Request", "").lower() == "true":
                    abort(403)
                flash("You don't have permission to perform that action.", "error")
                return redirect(url_for("dashboard.dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator
