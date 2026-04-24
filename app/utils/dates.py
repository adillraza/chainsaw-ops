"""Date / numeric coercion helpers used by the cache layer and BigQuery sync."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def safe_parse_date(date_str: Any) -> datetime | None:
    """Best-effort ISO date parser. Returns ``None`` for invalid / sentinel values."""
    if not date_str:
        return None

    if date_str is None or date_str == "" or date_str == "None":
        return None

    try:
        if isinstance(date_str, datetime):
            return date_str

        if isinstance(date_str, str):
            if date_str.lower() in {"null", "none", "invalid date", "", "0000-00-00", "0000-00-00 00:00:00"}:
                return None
            if date_str.startswith("0000-00-00"):
                return None
            if date_str.endswith("Z"):
                date_str = date_str.replace("Z", "+00:00")
            parsed_date = datetime.fromisoformat(date_str)
            if parsed_date.year < 1900 or parsed_date.year > 2100:
                return None
            return parsed_date
        return None
    except (ValueError, TypeError):
        return None


def parse_iso_datetime(value: Any) -> datetime | None:
    """Strict ISO-8601 parser used for BigQuery review payloads."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return None
    except (ValueError, TypeError):
        return None


def convert_decimal_to_float(value: Any) -> Any:
    """Coerce ``decimal.Decimal`` (returned by BigQuery NUMERIC fields) to ``float``
    so it can be stored in SQLite without losing precision warnings."""
    if value is None:
        return None
    if hasattr(value, "__class__") and "Decimal" in str(value.__class__):
        return float(value)
    return value
