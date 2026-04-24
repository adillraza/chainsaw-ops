"""Application configuration. Reads from environment variables (``.env`` is
loaded by :mod:`app` at import time)."""
from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Generate one and add it to .env (see env_template.txt)."
        )
    return value


_INSTANCE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance")


class Config:
    SECRET_KEY = _require("SECRET_KEY")

    # SQLite local DB (auth, login log, cache, reviews). Resolved relative to
    # the Flask instance folder regardless of CWD.
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("SQLALCHEMY_DATABASE_URI")
        or "sqlite:///" + os.path.join(_INSTANCE_PATH, "users.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # BigQuery
    GOOGLE_APPLICATION_CREDENTIALS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    BIGQUERY_PROJECT_ID = os.environ.get("BIGQUERY_PROJECT_ID", "chainsawspares-385722")

    # Retail Express API (not used yet; kept for compatibility with old config.py)
    RETAIL_EXPRESS_API_KEY = os.environ.get("RETAIL_EXPRESS_API_KEY")
    RETAIL_EXPRESS_BASE_URL = os.environ.get(
        "RETAIL_EXPRESS_BASE_URL", "https://api.retailexpress.com.au"
    )

    # Feature flags
    ENABLE_V2_UI = os.environ.get("ENABLE_V2_UI", "false").lower() in {"1", "true", "yes"}
