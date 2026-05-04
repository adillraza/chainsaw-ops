#!/usr/bin/env python3
"""CXone live-call bridge.

Long-polls the CXone ``/contacts/active`` endpoint, diffs against the set of
contacts we last saw, and POSTs synthetic webhook events to the
chainsaw-ops ``/api/calls/webhook`` so the live-call drawer lights up for
CXone calls the same way it does for RC PBX calls.

Why this exists
---------------
CXone (NICE inContact) has a ``/services/v34.0/subscriptions`` endpoint, but
its event-type vocabulary isn't publicly documented and the ones we tried
all returned generic InvalidParameter errors. CXone's official real-time
data path for telephony is the long-polling pattern (``/contacts/active``,
``/agents/sessions``, etc.) -- this script is the lightweight bridge that
turns that pull-style API into push events for our webhook.

Latency budget
--------------
  CXone state change -> /contacts/active reflects (≤1s)
  Poller picks up    -> POSTs to /api/calls/webhook   (1-3s poll interval)
  Drawer polls       -> swaps in new card             (≤3s)
  Total              -> ≤6s end-to-end, perceptibly real-time

Behaviour
---------
* OAuth: password grant against ``cxone.niceincontact.com/auth/token`` using
  the same Secret-Manager-stored creds the chainsaw-call-analyzer pipeline
  uses (CXONE_USERNAME / CXONE_PASSWORD / CXONE_CLIENT_ID / CXONE_CLIENT_SECRET).
  Refreshes a few minutes before expiry.
* Tracks ``{contactId: stateName}`` between polls.
* Emits one event per state-transition:
    - new contact, stateName='Queued'/'Routing'  → ``Inbound:Alerting``
    - new contact, stateName='Active'            → ``Inbound:Answered``
    - same contact, transitions to 'Active'      → ``Inbound:Answered``
    - contact gone from list                     → ``Inbound:Disconnected``
* POSTs form-encoded to ``/api/calls/webhook`` so the existing parser in
  app/blueprints/live_calls/routes.py picks it up unchanged.

Run forever; intended to be supervised by systemd on the chainsaw-ops VPS.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CXONE_AUTH_URL = "https://cxone.niceincontact.com/auth/token"
CXONE_API_BASE = os.environ.get("CXONE_API_BASE", "https://api-au1.niceincontact.com")
CXONE_ACTIVE_PATH = "/InContactAPI/services/v34.0/contacts/active"

WEBHOOK_URL = os.environ.get(
    "CHAINSAW_WEBHOOK_URL",
    "http://127.0.0.1:5001/api/calls/webhook",
)

POLL_INTERVAL_SECONDS = float(os.environ.get("CXONE_POLL_INTERVAL", "3"))
TOKEN_REFRESH_BUFFER_SECONDS = 120  # refresh this long before expiry
GCP_PROJECT = os.environ.get("GCP_PROJECT", "chainsawspares-385722")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s cxone_poller: %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _gcloud_secret(name: str) -> str:
    """Fetch a single secret value from GCP Secret Manager."""
    return subprocess.check_output(
        ["gcloud", "secrets", "versions", "access", "latest",
         f"--secret={name}", f"--project={GCP_PROJECT}"],
        text=True,
    ).strip()


def _load_creds() -> dict:
    if all(os.environ.get(k) for k in ("CXONE_USERNAME","CXONE_PASSWORD","CXONE_CLIENT_ID","CXONE_CLIENT_SECRET")):
        return {
            "username":      os.environ["CXONE_USERNAME"],
            "password":      os.environ["CXONE_PASSWORD"],
            "client_id":     os.environ["CXONE_CLIENT_ID"],
            "client_secret": os.environ["CXONE_CLIENT_SECRET"],
        }
    log.info("loading CXone creds from Secret Manager")
    return {
        "username":      _gcloud_secret("CXONE_USERNAME"),
        "password":      _gcloud_secret("CXONE_PASSWORD"),
        "client_id":     _gcloud_secret("CXONE_CLIENT_ID"),
        "client_secret": _gcloud_secret("CXONE_CLIENT_SECRET"),
    }


# ---------------------------------------------------------------------------
# CXone session
# ---------------------------------------------------------------------------

class CXoneSession:
    def __init__(self, creds: dict):
        self.creds = creds
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None

    def authenticate(self) -> None:
        r = requests.post(
            CXONE_AUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "password",
                "username":      self.creds["username"],
                "password":      self.creds["password"],
                "client_id":     self.creds["client_id"],
                "client_secret": self.creds["client_secret"],
            },
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        self.access_token = d["access_token"]
        self.token_expires_at = datetime.utcnow() + timedelta(seconds=d.get("expires_in", 3600))
        log.info("CXone auth OK; token valid until %s", self.token_expires_at.isoformat())

    def _refresh_if_needed(self) -> None:
        if (
            self.access_token is None
            or self.token_expires_at is None
            or self.token_expires_at - datetime.utcnow() < timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS)
        ):
            self.authenticate()

    def fetch_active_contacts(self) -> list[dict]:
        self._refresh_if_needed()
        r = requests.get(
            CXONE_API_BASE + CXONE_ACTIVE_PATH,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=15,
        )
        # Re-auth on 401 once and retry
        if r.status_code == 401:
            log.warning("401 from CXone, re-authenticating")
            self.authenticate()
            r = requests.get(
                CXONE_API_BASE + CXONE_ACTIVE_PATH,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=15,
            )
        r.raise_for_status()
        d = r.json()
        return d.get("activeContacts") or []


# ---------------------------------------------------------------------------
# Event mapping
# ---------------------------------------------------------------------------

def _state_to_event_type(state_name: str | None) -> str:
    """Map CXone stateName to our 'Direction:Status' string used by the drawer.

    The drawer treats anything containing 'Disconnected' as terminal; any
    other status is rendered as in-flight. We use 'Inbound:Alerting' for
    queued/ringing and 'Inbound:Answered' for active-with-agent so the drawer
    colour-codes the right way (amber pulse vs emerald).
    """
    if not state_name:
        return "Inbound:Setup"
    s = state_name.lower()
    if s in ("active", "answered", "with agent", "talking"):
        return "Inbound:Answered"
    if s in ("on hold", "hold"):
        return "Inbound:Hold"
    return "Inbound:Alerting"  # Queued, Routing, Pre-Queued, etc.


def _post_event(contact: dict, override_event_type: str | None = None) -> None:
    """Push a single CXone-shaped event to the chainsaw-ops webhook.

    The receiver's parser already handles flat-key CXone shape; we just need
    to make sure the ``eventType`` field matches the 'Inbound:Status' format
    the drawer's _parse_event expects. We prepend that as the contact's flat
    eventType so it ends up in CallEvent.event_type.
    """
    state_name = contact.get("stateName")
    event_type = override_event_type or _state_to_event_type(state_name)
    payload = {
        "eventType":         event_type,
        "contactId":         str(contact.get("contactId", "")),
        "masterContactId":   str(contact.get("masterContactId", "")),
        "fromAddress":       contact.get("fromAddress", ""),
        "toAddress":         contact.get("toAddress", ""),
        "skill":             contact.get("skillName", ""),
        "stateName":         state_name or "",
        "agentName":         f"{contact.get('firstName','')} {contact.get('lastName','')}".strip(),
        "agentId":           str(contact.get("agentId", "")),
        "mediaTypeName":     contact.get("mediaTypeName", ""),
    }
    try:
        r = requests.post(WEBHOOK_URL, data=payload, timeout=5)
        if r.status_code >= 300:
            log.warning("webhook %s -> %s %s", WEBHOOK_URL, r.status_code, r.text[:120])
        else:
            log.info(
                "→ %s  contact=%s  from=%s  state=%s  evt=%s",
                event_type, payload["contactId"], payload["fromAddress"], state_name, event_type,
            )
    except Exception as e:
        log.error("webhook POST failed: %s", e)


# ---------------------------------------------------------------------------
# Diff loop
# ---------------------------------------------------------------------------

def main_loop(session: CXoneSession) -> None:
    seen: dict[str, dict] = {}  # contactId -> last contact dict

    log.info("starting CXone poller; interval=%ss webhook=%s", POLL_INTERVAL_SECONDS, WEBHOOK_URL)
    while True:
        try:
            contacts = session.fetch_active_contacts()
            current_ids = {str(c.get("contactId")) for c in contacts if c.get("contactId")}
            current_by_id = {str(c.get("contactId")): c for c in contacts if c.get("contactId")}

            # New + state-transitions
            for cid, c in current_by_id.items():
                prev = seen.get(cid)
                if prev is None:
                    _post_event(c)
                else:
                    # Same contact, but state may have moved (Queued -> Active)
                    if c.get("stateName") != prev.get("stateName"):
                        _post_event(c)

            # Contacts that have disappeared since last poll → Disconnected
            for cid in set(seen) - current_ids:
                last = seen[cid]
                _post_event(last, override_event_type="Inbound:Disconnected")

            seen = current_by_id
        except requests.exceptions.RequestException as e:
            log.warning("CXone fetch failed: %s; will retry", e)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        time.sleep(POLL_INTERVAL_SECONDS)


def _signal_handler(sig, frame):
    log.info("received signal %s; shutting down", sig)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    creds = _load_creds()
    session = CXoneSession(creds)
    session.authenticate()
    main_loop(session)
