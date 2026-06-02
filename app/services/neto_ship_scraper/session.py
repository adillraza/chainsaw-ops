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


def create_session(username: str, password: str) -> ScraperSession:
    """Login to the Neto cPanel; raise on failure."""
    s = ScraperSession()
    resp = s.get(BASE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title else ""
    if "just a moment" in title.lower() or resp.status_code == 403:
        raise RuntimeError(f"Cloudflare blocked the request (status={resp.status_code}, title={title!r})")

    for form in soup.find_all("form"):
        if form.find("input", {"type": "password"}) and form.find("input", {"type": "text"}):
            data = {inp.get("name"): inp.get("value", "")
                    for inp in form.find_all("input") if inp.get("name")}
            for inp in form.find_all("input"):
                name, itype = inp.get("name", ""), inp.get("type", "text")
                if itype == "text":
                    data[name] = username
                elif itype == "password":
                    data[name] = password
            action = form.get("action", f"{BASE_URL}/login")
            if not action.startswith("http"):
                action = f"https://www.chainsawspares.com.au{action}"
            s.post(action, data=data, allow_redirects=True)
            break

    # Verify: a real authenticated cPanel page is large; the login form is ~9KB.
    check = s.get(f"{BASE_URL}/shippingcostmgr").text
    if len(check) < 40000 or "shippingcostmgr/view?id=" not in check:
        raise RuntimeError("cPanel login failed (got login page) — check creds or wait out a rate-limit")
    return s
