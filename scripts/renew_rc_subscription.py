#!/usr/bin/env python3
"""Renew the RingCentral telephony.sessions webhook subscription.

RC subscriptions max out at 7 days. This script:

  1. Pulls the JWT credential from GCP Secret Manager (`rc-credentials`).
  2. Exchanges it for an access token.
  3. Lists current subscriptions on this app.
  4. Renews the one whose deliveryMode.address matches our webhook URL
     (PUT /subscription/{id}/renew).
  5. If no matching subscription exists (e.g. expired and was auto-deleted),
     creates a fresh one.

Run via systemd timer on the chainsaw-ops VPS, daily.

Idempotent. Safe to run on demand.

Exit codes:
  0  renewed or recreated successfully
  1  RC API error
  2  configuration / credentials error
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Optional

import requests

WEBHOOK_URL = os.environ.get(
    "RC_WEBHOOK_URL",
    "https://ops.jonoandjohno.com.au/api/calls/webhook",
)
EVENT_FILTERS = ["/restapi/v1.0/account/~/telephony/sessions"]
EXPIRES_IN_SECONDS = 604800  # 7 days, RC max
RC_BASE = "https://platform.ringcentral.com"


def _load_creds() -> dict:
    """Pull RC creds from Secret Manager, fall back to a local JSON file."""
    if path := os.environ.get("RC_CREDENTIALS_FILE"):
        with open(path) as f:
            return json.load(f)
    # Use gcloud CLI to fetch the secret. Avoids the google-cloud-secret-manager
    # dependency for what's a one-shot script.
    import subprocess
    out = subprocess.check_output([
        "gcloud", "secrets", "versions", "access", "latest",
        "--secret=rc-credentials",
        "--project=chainsawspares-385722",
    ], text=True)
    return json.loads(out)


def _get_token(creds: dict) -> str:
    basic = base64.b64encode(f"{creds['clientId']}:{creds['clientSecret']}".encode()).decode()
    r = requests.post(
        f"{RC_BASE}/restapi/oauth/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": creds["jwt"]["ABJWT"]},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _find_our_subscription(token: str) -> Optional[dict]:
    r = requests.get(
        f"{RC_BASE}/restapi/v1.0/subscription",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    for sub in r.json().get("records", []):
        addr = (sub.get("deliveryMode") or {}).get("address")
        if addr == WEBHOOK_URL:
            return sub
    return None


def _renew(token: str, sub_id: str) -> dict:
    r = requests.post(
        f"{RC_BASE}/restapi/v1.0/subscription/{sub_id}/renew",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _create(token: str) -> dict:
    r = requests.post(
        f"{RC_BASE}/restapi/v1.0/subscription",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "eventFilters": EVENT_FILTERS,
            "deliveryMode": {"transportType": "WebHook", "address": WEBHOOK_URL},
            "expiresIn": EXPIRES_IN_SECONDS,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    try:
        creds = _load_creds()
        token = _get_token(creds)
    except Exception as e:
        print(f"FATAL: could not authenticate with RingCentral: {e}", file=sys.stderr)
        return 2

    try:
        existing = _find_our_subscription(token)
        if existing is None:
            print("No matching subscription; creating new one...")
            new_sub = _create(token)
            print(
                f"Created sub {new_sub['id']} status={new_sub['status']} "
                f"expires={new_sub['expirationTime']} "
                f"disabledFilters={len(new_sub.get('disabledFilters', []))}"
            )
            return 0

        sub_id = existing["id"]
        print(f"Found existing sub {sub_id} status={existing['status']} expires={existing.get('expirationTime')}")
        renewed = _renew(token, sub_id)
        print(
            f"Renewed sub {renewed['id']} status={renewed['status']} "
            f"new expires={renewed['expirationTime']}"
        )
    except requests.HTTPError as e:
        print(f"RC API error: {e.response.status_code} {e.response.text[:300]}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
