"""Authorisation helpers."""
from __future__ import annotations


def has_admin_access(user) -> bool:
    """Backwards-compatible admin check.

    The legacy ``User`` model had two overlapping flags (``is_admin`` and
    ``role``); we honour either so older accounts keep working.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_admin", False):
        return True
    if getattr(user, "role", None) == "admin":
        return True
    return False
