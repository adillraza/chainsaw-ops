"""UI helpers.

The legacy Bootstrap shell was retired in Phase 5b — every page now extends
``layouts/base.html`` (Tailwind/Preline/HTMX). These shims are kept so older
callers and templates that still reference ``use_v2_ui`` / ``layout_template``
keep working without churn while the legacy code paths are deleted.
"""
from __future__ import annotations


def use_v2_ui() -> bool:
    return True


def layout_template() -> str:
    return "layouts/base.html"
