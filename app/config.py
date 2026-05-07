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

    # Microsoft Entra (Azure AD) SSO. Reuses the SHAREPOINT_* creds the
    # email backfill loads — same Azure app registration, just in
    # delegated auth-code flow here instead of client-credentials. If
    # any of the three are missing, the /auth/microsoft/login route
    # flashes "not configured" and falls back to the password form, so
    # dev databases without Entra access aren't blocked.
    MS_CLIENT_ID     = os.environ.get("SHAREPOINT_CLIENT_ID")
    MS_CLIENT_SECRET = os.environ.get("SHAREPOINT_CLIENT_SECRET")
    MS_TENANT_ID     = os.environ.get("SHAREPOINT_TENANT_ID")
    # Optional explicit redirect URI override. Set this in prod .env
    # (``MS_REDIRECT_URI=https://ops.jonoandjohno.com.au/auth/microsoft/callback``)
    # so the redirect URI sent to Azure matches what's registered there
    # regardless of how Flask is sitting behind nginx. Leave unset on
    # local dev and the route falls back to url_for(_external=True).
    MS_REDIRECT_URI  = os.environ.get("MS_REDIRECT_URI")

    # Feature flags
    ENABLE_V2_UI = os.environ.get("ENABLE_V2_UI", "false").lower() in {"1", "true", "yes"}
