"""Authorization layer: capabilities, roles, and access helpers.

This package is the single source of truth for *what* a user can do.

- ``capabilities`` defines the authoritative catalog of capability strings
  (developer-controlled -- they match code paths that exist).
- Roles are stored in the database (``app.models.role.Role``) so admins can
  edit ``role -> capabilities`` mappings at runtime via the admin UI.
- ``abilities`` exposes ``require_capability`` (route decorator) and a
  ``user.can(cap)`` helper that every route/template should use instead of
  reading ``user.role`` directly.

Public surface re-exported for convenience.
"""
from app.auth.capabilities import (
    CAPABILITIES,
    CAPABILITY_GROUPS,
    SYSTEM_ROLE_DEFAULTS,
    is_valid_capability,
)
from app.auth.abilities import require_capability, user_can

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_GROUPS",
    "SYSTEM_ROLE_DEFAULTS",
    "is_valid_capability",
    "require_capability",
    "user_can",
]
