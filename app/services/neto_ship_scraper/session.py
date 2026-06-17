"""Authenticated Neto cPanel session (curl_cffi Chrome-TLS impersonation).

Mirrors the proven pattern from chainsaw-functions/stock-adjustment-scraper —
curl_cffi clears Cloudflare from cloud/VPS IPs; cloudscraper is the fallback.
Logs in via the legacy username/password form and verifies success.
"""
from __future__ import annotations

import logging
from bs4 import BeautifulSoup

BASE_URL = "https://www.chainsawspares.com.au/_cpanel"
log = logging.getLogger(__name__)


class ScraperSession:
    def __init__(self):
        try:
            from curl_cffi.requests import Session
            self._session = Session(impersonate="chrome124")
            self.engine = "curl_cffi"
        except ImportError:
            import cloudscraper
            self._session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "desktop": True})
            self.engine = "cloudscraper"

    def get(self, url, **kw):
        return self._session.get(url, **kw)

    def post(self, url, **kw):
        return self._session.post(url, **kw)


def _submit_form(s, form, username, password, take_over=False):
    """POST a login/takeover form: preserve hidden fields, fill creds, and
    optionally tick take_over_session=y to kick a concurrent session."""
    data = {inp.get("name"): inp.get("value", "")
            for inp in form.find_all("input") if inp.get("name")}
    for inp in form.find_all("input"):
        name, itype = inp.get("name", ""), inp.get("type", "text")
        if itype == "text" or name == "username":
            data[name] = username
        elif itype == "password" or name == "password":
            data[name] = password
    if take_over:
        # Neto's single-session takeover checkbox.
        data["take_over_session"] = "y"
    if "username" not in data:
        data["username"] = username
    if "password" not in data:
        data["password"] = password
    action = form.get("action", f"{BASE_URL}/login")
    if not action.startswith("http"):
        action = f"https://www.chainsawspares.com.au{action}"
    return s.post(action, data=data, allow_redirects=True)


def create_session(username: str, password: str) -> ScraperSession:
    """Login to the Neto cPanel, taking over any concurrent session if needed.

    Neto enforces one session per user; logging in while a session exists
    returns an "another session exists" page with a ``take_over_session``
    checkbox. We detect that and re-submit with the takeover flag (this kicks
    whatever else is logged in as this user — we share the account with the
    daily scraper and any human cPanel session)."""
    s = ScraperSession()
    # GET the legacy login page directly. As of 2026-06-16 Maropost moved cPanel
    # behind Maropost Identity (Keycloak SSO): GET {BASE_URL} now 302-redirects to
    # identity.maropost.com. The legacy username/password form still lives at
    # {BASE_URL}/login (<form id="loginform">), so hit it directly. (Legacy login
    # is deprecated by Maropost — when removed, the verify below fails loudly and
    # this must migrate to the OIDC/Keycloak flow.)
    resp = s.get(f"{BASE_URL}/login")
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title else ""
    if "just a moment" in title.lower() or resp.status_code == 403:
        raise RuntimeError(f"Cloudflare blocked the request (status={resp.status_code}, title={title!r})")

    # Step 1: normal username/password login. Prefer the legacy form explicitly
    # (id="loginform") to avoid POSTing to a Maropost Identity SSO form.
    resp2 = None
    login_form = soup.find("form", id="loginform")
    if login_form is None:
        for form in soup.find_all("form"):
            if form.find("input", {"type": "password"}):
                login_form = form
                break
    if login_form is not None:
        resp2 = _submit_form(s, login_form, username, password)

    # Step 2: if Neto reports a concurrent session, take it over and retry.
    if resp2 is not None:
        soup2 = BeautifulSoup(resp2.text, "html.parser")
        takeover_form = None
        for form in soup2.find_all("form"):
            if form.find("input", {"name": "take_over_session"}):
                takeover_form = form
                break
        if takeover_form is not None or "another session exists" in resp2.text.lower():
            if takeover_form is not None:
                _submit_form(s, takeover_form, username, password, take_over=True)

    # Verify: a real authenticated cPanel page is large; the login form is ~9KB.
    check = s.get(f"{BASE_URL}/shippingcostmgr").text
    if len(check) < 40000 or "shippingcostmgr/view?id=" not in check:
        raise RuntimeError(
            "cPanel login failed. Either the password rotated (update Secret Manager "
            "'neto-cpanel-password'), or a concurrent-session takeover didn't go through."
        )
    return s
