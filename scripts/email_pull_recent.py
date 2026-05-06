"""Ad-hoc helper: pull last N messages involving a specific email address.

Mirrors what the call_event live-merge does on the Customer 360 card —
gives the agent a near-real-time view that's fresher than the last
hourly sync. Two modes:

  * --bq-only      : just query BigQuery (fast, what the panel does
                     by default since the hourly sync is recent enough)
  * --live         : ALSO query Microsoft Graph directly for the most
                     recent N messages, dedupe against BQ by message_id,
                     and INSERT the missing ones. Triggered when an
                     agent explicitly wants "freshest possible view"
                     (e.g. customer just sent an email mid-call).

Usage:
    python3 email_pull_recent.py rowanfrawley@hotmail.com
    python3 email_pull_recent.py rowanfrawley@hotmail.com --live
    python3 email_pull_recent.py rowanfrawley@hotmail.com --top 20

Future: this becomes a Flask endpoint called from the Customer 360
panel via HTMX, similar to the active-call lookup.
"""
from __future__ import annotations

import argparse, json, subprocess, sys, time
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

from google.cloud import bigquery

PROJECT = "chainsawspares-385722"
DATASET = "email_archive"
MAILBOX = "sales@jonoandjohno.com.au"

# Reuse helpers from the backfill script
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_backfill import (
    secret, get_token, graph_get, list_folders, to_row, insert_batch, FIELDS, SKIP_FOLDERS,
)


def query_bq(bq, customer_email, top):
    sql = f"""
    SELECT message_id, conversation_id, parent_folder_name, direction,
           subject, from_address, from_name, to_addresses, received_at,
           body_preview, web_link, is_automated, has_attachments
    FROM `{PROJECT}.{DATASET}.messages`
    WHERE LOWER(from_address) = LOWER(@email)
       OR LOWER(@email) IN UNNEST(to_addresses)
    ORDER BY received_at DESC
    LIMIT @top
    """
    return list(bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("email", "STRING", customer_email),
        bigquery.ScalarQueryParameter("top",   "INT64",  top),
    ])).result())


def live_topup(bq, customer_email, top):
    """Pull recent messages for this address straight from Graph and
    insert any that aren't already in BQ. Returns count inserted."""
    token = get_token()

    # Two filtered searches: from=customer, to/cc=customer.
    # Graph's $search supports message free-text but $filter is stricter.
    # Use $search with 'from:' and 'to:' KQL keywords for accuracy.
    search_queries = [
        f'"from:{customer_email}"',
        f'"to:{customer_email}"',
    ]

    # Get folder map for parent_folder_name + skip filter
    folder_map = list_folders(MAILBOX, token)
    skip_ids = {fid for fid, name in folder_map.items() if name in SKIP_FOLDERS}

    # Existing message_ids for this customer to avoid re-inserting
    existing_ids = {r.message_id for r in query_bq(bq, customer_email, top * 2)}

    ingested_at = datetime.now(timezone.utc)
    inserted = 0
    for q in search_queries:
        url = (f"https://graph.microsoft.com/v1.0/users/{quote(MAILBOX)}/messages"
               f"?$search={quote(q)}&$top={top}&$select={FIELDS}")
        try:
            data = graph_get(url, token)
        except Exception as e:
            print(f"  search '{q}' failed: {e}", file=sys.stderr)
            continue
        rows = []
        for m in data.get("value", []):
            if "id" not in m or m["id"] in existing_ids: continue
            if m.get("parentFolderId") in skip_ids: continue
            rows.append(to_row(m, MAILBOX, folder_map, ingested_at))
            existing_ids.add(m["id"])
        inserted += insert_batch(bq, rows)
    return inserted


def fmt_row(r):
    arrow = "←" if r.direction == "inbound" else "→"
    flag = " [auto]" if r.is_automated else ""
    when = r.received_at.strftime("%Y-%m-%d %H:%M") if r.received_at else "?"
    subj = (r.subject or "(no subject)")[:80]
    return f"  {when}  {arrow} {subj}{flag}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("email", help="customer's email address")
    p.add_argument("--top",  type=int, default=10)
    p.add_argument("--live", action="store_true",
                   help="also top-up from Graph for freshest view")
    p.add_argument("--bq-only", action="store_true",
                   help="(default) skip the Graph top-up")
    args = p.parse_args()

    bq = bigquery.Client(project=PROJECT)

    if args.live:
        print(f"Live top-up from Graph for {args.email}...", file=sys.stderr)
        n = live_topup(bq, args.email, args.top)
        print(f"  inserted {n} new messages", file=sys.stderr)

    rows = query_bq(bq, args.email, args.top)
    print(f"\n=== {len(rows)} messages for {args.email} ===\n")
    for r in rows:
        print(fmt_row(r))
        if r.web_link:
            print(f"     {r.web_link[:120]}")
    print()

if __name__ == "__main__":
    main()
