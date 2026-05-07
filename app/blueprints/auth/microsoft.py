"""Microsoft Entra (Azure AD) sign-in via OIDC auth-code flow.

Two routes:

* ``GET /auth/microsoft/login``    — kicks off the auth-code flow and
  302s the browser to Microsoft's login page.
* ``GET /auth/microsoft/callback`` — Microsoft redirects back here with
  ``?code=…``; we exchange it for an ID token, look the user up by
  ``oid`` (or fall back to ``upn``), and ``login_user`` them.

App registration: reuses the existing ``chainsaw-ops-sharepoint-reader``
app (the one that powers the email backfill). The same client id +
secret + tenant id are used; only the *flow* is different (delegated
auth-code here, vs client-credentials over there).

Security notes:

* MSAL's ``initiate_auth_code_flow`` writes a per-session ``state`` and
  PKCE ``code_verifier`` into the Flask session. The callback validates
  both, so a malicious URL with someone else's ``code`` can't be
  redeemed in our session.
* New users land on the ``viewer`` role (read-only Customer 360). An
  admin promotes them from the User Management page.
* Local password login stays available — Microsoft sign-in is layered
  on top, not a replacement, so the bootstrap admin can break-glass if
  Entra ever misbehaves.
"""
from __future__ import annotations

import logging
from datetime import datetime

import msal
from flask import (current_app, flash, redirect, request, session, url_for)
from flask_login import login_user

from app.blueprints.auth import auth_bp
from app.extensions import db
from app.models.user import LoginLog, User

log = logging.getLogger(__name__)

# OIDC scopes for sign-in. ``openid`` + ``profile`` + ``email`` give us
# the standard claims (``oid``, ``preferred_username``, ``name``,
# ``email``) we need to identify the user. ``User.Read`` is delegated
# Graph access — useful later for richer profile info; cheap to ask for.
SCOPES = ["User.Read"]

# Where the user lands after a successful sign-in. Kept generic so
# changing the post-login destination doesn't ripple here.
POST_LOGIN_ENDPOINT = "dashboard.dashboard"


def _msal_app():
    """Build a fresh confidential-client app each request.

    Cheap to construct; avoids stale state between workers. ``authority``
    pins us to the JJ tenant — public Microsoft accounts can't sign in.
    """
    cfg = current_app.config
    return msal.ConfidentialClientApplication(
        client_id=cfg["MS_CLIENT_ID"],
        client_credential=cfg["MS_CLIENT_SECRET"],
        authority=f"https://login.microsoftonline.com/{cfg['MS_TENANT_ID']}",
    )


def _redirect_uri() -> str:
    """The fully-qualified callback URL Microsoft redirects back to.

    Built off the deployed host so dev (``http://localhost:5001/…``) and
    prod (``https://ops.jonoandjohno.com.au/…``) Just Work without an
    env var. Must match a Redirect URI registered on the Azure app.
    """
    return url_for("auth.microsoft_callback", _external=True)


# ---------------------------------------------------------------------------
# /auth/microsoft/login
# ---------------------------------------------------------------------------

@auth_bp.route("/auth/microsoft/login")
def microsoft_login():
    if not current_app.config.get("MS_CLIENT_ID"):
        flash("Microsoft sign-in is not configured on this server.", "error")
        return redirect(url_for("auth.login"))

    flow = _msal_app().initiate_auth_code_flow(
        scopes=SCOPES,
        redirect_uri=_redirect_uri(),
    )
    # Stash the whole flow dict in the session — MSAL needs the same
    # state + verifier on the callback to validate the response.
    session["ms_auth_flow"] = flow
    return redirect(flow["auth_uri"])


# ---------------------------------------------------------------------------
# /auth/microsoft/callback
# ---------------------------------------------------------------------------

@auth_bp.route("/auth/microsoft/callback")
def microsoft_callback():
    flow = session.pop("ms_auth_flow", None)
    if not flow:
        flash("Microsoft sign-in session expired — please try again.", "error")
        return redirect(url_for("auth.login"))

    try:
        result = _msal_app().acquire_token_by_auth_code_flow(
            flow, request.args.to_dict(),
        )
    except Exception as exc:
        log.warning("MSAL token exchange threw: %s", exc)
        flash("Microsoft sign-in failed. Please try again or use a "
              "username + password.", "error")
        return redirect(url_for("auth.login"))

    if "error" in result:
        log.warning("MSAL error: %s — %s", result.get("error"),
                    result.get("error_description"))
        flash("Microsoft sign-in was cancelled or rejected.", "error")
        return redirect(url_for("auth.login"))

    claims = result.get("id_token_claims") or {}
    oid = claims.get("oid")
    upn = (claims.get("preferred_username") or claims.get("upn") or "").lower()
    name = claims.get("name") or upn

    if not oid or not upn:
        log.warning("MSAL response missing oid/upn: %s", claims.keys())
        flash("Microsoft sign-in didn't return a usable identity.", "error")
        return redirect(url_for("auth.login"))

    user = _resolve_or_create_user(oid=oid, upn=upn, display_name=name)
    if user is None:
        flash("Sign-in succeeded but no matching user record could be "
              "created. Ask an admin to set up your account.", "error")
        return redirect(url_for("auth.login"))

    login_user(user, remember=True)

    db.session.add(LoginLog(
        user_id=user.id,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    ))
    user.last_microsoft_login_at = datetime.utcnow()
    db.session.commit()

    log.info("microsoft sign-in: user_id=%s upn=%s role=%s",
             user.id, user.microsoft_upn, user.role)
    return redirect(url_for(POST_LOGIN_ENDPOINT))


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

DEFAULT_SSO_ROLE = "viewer"


def _resolve_or_create_user(*, oid: str, upn: str, display_name: str) -> User | None:
    """Return the local User row corresponding to a Microsoft identity.

    Order of resolution:
    1. ``microsoft_oid`` exact match — the stable, rename-proof key.
       Always wins when present.
    2. ``microsoft_upn`` match — covers SSO users who renamed in Entra
       between sessions.
    3. ``username == upn`` — the migration path for pre-existing local
       users. Their first SSO sign-in links the row.
    4. Brand new user — auto-created with ``role="viewer"``.

    Returns ``None`` only if a database error prevents creation; callers
    treat that as a soft sign-in failure.
    """
    user = User.query.filter_by(microsoft_oid=oid).first()
    if user:
        # Keep display fields fresh in case Entra has newer data.
        user.microsoft_upn = upn
        user.display_name = display_name
        return user

    # Linkable existing rows — match by UPN first (we may have stored it
    # from an earlier sign-in that was later un-linked), then by the
    # legacy ``username`` column which agents currently log in with.
    user = (User.query.filter_by(microsoft_upn=upn).first()
            or User.query.filter(db.func.lower(User.username) == upn).first())
    if user:
        user.microsoft_oid = oid
        user.microsoft_upn = upn
        if not user.display_name:
            user.display_name = display_name
        return user

    # New SSO user — create the row with the default low-privilege role.
    try:
        user = User(
            username=upn,
            microsoft_oid=oid,
            microsoft_upn=upn,
            display_name=display_name,
            role=DEFAULT_SSO_ROLE,
            is_admin=False,
        )
        db.session.add(user)
        db.session.flush()
        log.info("microsoft sign-in: auto-created user upn=%s role=%s",
                 upn, DEFAULT_SSO_ROLE)
        return user
    except Exception as exc:
        log.warning("microsoft sign-in: auto-create failed for %s: %s",
                    upn, exc)
        db.session.rollback()
        return None
