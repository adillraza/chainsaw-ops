"""Backfill sales@ mailbox (whole mailbox) into BigQuery.

Walks the top-level Microsoft Graph /messages/delta endpoint so we
catch every folder including the agent-curated ones (Customer
Correspondence ONLY, NETO invoices etc, eBay direct emails, etc.) —
not just Inbox + Sent Items, which are nearly empty in this account
because new mail gets filed away daily.

Filters at ingestion:
* Drop messages in Drafts / Outbox / Junk Email / Deleted Items
  / Conversation History / RSS Feeds / Sync Issues / Voice Mails.
* Keep everything else, including Neto auto-emails — those are
  useful for customer 360 context per the user's spec.

Direction inference:
* from_address == sales@... → outbound
* otherwise               → inbound

The script gets a deltaLink at the end of the walk and stashes it in
email_archive.sync_state. The hourly sync uses that token to fetch
only changed messages on subsequent runs.

Usage:
    python3 email_backfill.py [--limit N] [--reset]
"""
from __future__ import annotations

import argparse, json, re, subprocess, sys, time
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from google.cloud import bigquery

PROJECT  = "chainsawspares-385722"
DATASET  = "email_archive"
MAILBOX  = "sales@jonoandjohno.com.au"

FIELDS = ",".join([
    "id", "conversationId", "internetMessageId",
    "subject", "from", "toRecipients", "ccRecipients", "bccRecipients",
    "receivedDateTime", "sentDateTime",
    "bodyPreview", "body",
    "hasAttachments", "isDraft", "isRead",
    "importance", "webLink",
    "parentFolderId",
])

# Folders we never want in the KB / customer panel.
SKIP_FOLDERS = {
    "Drafts", "Outbox", "Junk Email", "Deleted Items",
    "Conversation History", "RSS Feeds", "Sync Issues",
    "Voice Mails",            # audio attachments, not text correspondence
    "Recoverable Items",
}

# Subject patterns that mark a message as automated. We still INGEST these
# (user wants Neto auto-emails for context) but flag is_automated=true so
# the UI can group/demote them.
AUTO_PATTERNS = [
    re.compile(r"^Jono & Johno PTY LTD (Order Receipt|Tax Invoice|Order|Refund) #?", re.I),
    re.compile(r"^Message From Jono & Johno PTY LTD Related To Order", re.I),
    re.compile(r"^New Jono & Johno PTY LTD User Account Created", re.I),
    re.compile(r"^Jono & Johno PTY LTD Password Reset", re.I),
    re.compile(r"^Your.*tracking.*update", re.I),
    re.compile(r"^Delivery (failed|delayed|notification|update)", re.I),
    re.compile(r"^(Out of office|Auto-?reply)", re.I),
    re.compile(r"^Undeliverable:", re.I),
    re.compile(r"^Mail (delivery failed|delivery system)", re.I),
]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def secret(name):
    return subprocess.check_output(
        ["/Users/adil/google-cloud-sdk/bin/gcloud", "secrets", "versions", "access", "latest",
         "--secret", name, "--project", PROJECT],
        stderr=subprocess.DEVNULL).decode().strip()

def get_token():
    body = (f"grant_type=client_credentials&client_id={secret('sharepoint-client-id')}"
            f"&client_secret={quote(secret('sharepoint-client-secret'))}"
            f"&scope={quote('https://graph.microsoft.com/.default')}").encode()
    r = urlopen(
        f"https://login.microsoftonline.com/{secret('sharepoint-tenant-id')}/oauth2/v2.0/token",
        data=body, timeout=30)
    return json.loads(r.read())["access_token"]

# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def graph_get(url, token):
    """GET with retry on 429 + transient 5xx. Returns parsed JSON or raises."""
    for attempt in range(5):
        req = Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Prefer": "outlook.body-content-type=\"text\"",
        })
        try:
            with urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10"))
                print(f"    429 — sleep {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code in (502, 503, 504):
                print(f"    {e.code} — backoff {2**attempt}s", file=sys.stderr)
                time.sleep(2**attempt)
                continue
            raise
        except Exception as e:
            print(f"    {type(e).__name__}: {e} — backoff {2**attempt}s", file=sys.stderr)
            time.sleep(2**attempt)
    raise RuntimeError(f"giving up after retries: {url[:120]}")

def list_folders(mailbox, token):
    """Return {folder_id: displayName} for the WHOLE folder tree (recursive)."""
    out = {}
    queue = [f"https://graph.microsoft.com/v1.0/users/{quote(mailbox)}/mailFolders"
            "?$top=200&$select=id,displayName,childFolderCount"]
    while queue:
        url = queue.pop(0)
        data = graph_get(url, token)
        for f in data.get("value", []):
            out[f["id"]] = f["displayName"]
            if f.get("childFolderCount", 0) > 0:
                queue.append(f"https://graph.microsoft.com/v1.0/users/{quote(mailbox)}"
                             f"/mailFolders/{f['id']}/childFolders?$top=200&$select=id,displayName,childFolderCount")
        if data.get("@odata.nextLink"):
            queue.append(data["@odata.nextLink"])
    return out

# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

def _addr(person):
    if not person: return ("", "")
    ea = person.get("emailAddress") or {}
    return (ea.get("address", "").lower(), ea.get("name", ""))

def _addrs(people):
    return [_addr(p)[0] for p in (people or []) if _addr(p)[0]]

def _strip_html(html):
    if not html: return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))

def _is_automated(subject):
    return any(p.search(subject or "") for p in AUTO_PATTERNS)

def to_row(msg, mailbox, folder_id_to_name, ingested_at):
    from_addr, from_name = _addr(msg.get("from"))
    body = msg.get("body") or {}
    body_html = body.get("content") if body.get("contentType") == "html" else None
    body_text = body.get("content") if body.get("contentType") == "text" else None
    if body_html and not body_text:
        body_text = _strip_html(body_html)
    parent_id = msg.get("parentFolderId")
    return {
        "message_id":          msg["id"],
        "conversation_id":     msg.get("conversationId"),
        "internet_message_id": msg.get("internetMessageId"),
        "mailbox":             mailbox,
        "parent_folder_id":    parent_id,
        "parent_folder_name":  folder_id_to_name.get(parent_id),
        "direction":           "outbound" if from_addr == mailbox else "inbound",
        "subject":             msg.get("subject") or "",
        "from_address":        from_addr or None,
        "from_name":           from_name or None,
        "to_addresses":        _addrs(msg.get("toRecipients")),
        "cc_addresses":        _addrs(msg.get("ccRecipients")),
        "bcc_addresses":       _addrs(msg.get("bccRecipients")),
        "received_at":         (msg.get("receivedDateTime") or "").replace("Z", "+00:00") or None,
        "sent_at":             (msg.get("sentDateTime")     or "").replace("Z", "+00:00") or None,
        "body_preview":        msg.get("bodyPreview"),
        "body_text":           body_text,
        "body_html":           body_html,
        "has_attachments":     bool(msg.get("hasAttachments")),
        "is_draft":            bool(msg.get("isDraft")),
        "is_read":             bool(msg.get("isRead")),
        "is_automated":        _is_automated(msg.get("subject")),
        "importance":          msg.get("importance"),
        "web_link":            msg.get("webLink"),
        "ingested_at":         ingested_at.isoformat(),
    }

# ---------------------------------------------------------------------------
# BQ
# ---------------------------------------------------------------------------

def insert_batch(bq, rows):
    if not rows: return 0
    errors = bq.insert_rows_json(f"{PROJECT}.{DATASET}.messages", rows)
    if errors:
        # Log first few errors then continue — typical cause is a single
        # message with a malformed timestamp; better to log and move on
        # than to crash a 250k-message backfill on row #137,492.
        print(f"  ⚠ BQ insert: {len(errors)} row errors. First: {errors[0]}", file=sys.stderr)
        return len(rows) - len(errors)
    return len(rows)

def load_resume_token(bq, folder_key):
    rows = list(bq.query(f"""
        SELECT delta_link FROM `{PROJECT}.{DATASET}.sync_state`
        WHERE mailbox = @mb AND folder = @fld
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mb",  "STRING", MAILBOX),
        bigquery.ScalarQueryParameter("fld", "STRING", folder_key),
    ])).result())
    return rows[0].delta_link if rows else None

def save_delta(bq, folder_key, delta_link, total):
    bq.query(f"""
        MERGE `{PROJECT}.{DATASET}.sync_state` T
        USING (SELECT @mb AS mailbox, @fld AS folder, @dl AS delta_link,
                      CURRENT_TIMESTAMP() AS last_synced_at, @tot AS messages_seen) S
        ON T.mailbox = S.mailbox AND T.folder = S.folder
        WHEN MATCHED THEN UPDATE SET delta_link=S.delta_link,
              last_synced_at=S.last_synced_at, messages_seen=S.messages_seen
        WHEN NOT MATCHED THEN INSERT ROW
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mb",  "STRING", MAILBOX),
        bigquery.ScalarQueryParameter("fld", "STRING", folder_key),
        bigquery.ScalarQueryParameter("dl",  "STRING", delta_link),
        bigquery.ScalarQueryParameter("tot", "INT64",  total),
    ])).result()

def reset_state(bq):
    bq.query(f"""
        DELETE FROM `{PROJECT}.{DATASET}.sync_state` WHERE mailbox = @mb
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mb",  "STRING", MAILBOX),
    ])).result()
    print(f"  reset sync_state for {MAILBOX}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def walk_folder(bq, folder_id, folder_name, folder_map, fresh_token, limit_remaining):
    """Walk one folder's delta endpoint to completion, save deltaLink. Returns
    (msgs_seen, msgs_inserted)."""
    folder_key = folder_id   # use Graph folder id as the sync_state key
    delta_url = load_resume_token(bq, folder_key)
    if delta_url:
        print(f"  resuming '{folder_name}' from saved deltaLink", file=sys.stderr)
    else:
        delta_url = (f"https://graph.microsoft.com/v1.0/users/{quote(MAILBOX)}"
                     f"/mailFolders/{folder_id}/messages/delta?$select={FIELDS}&$top=999")

    ingested_at = datetime.now(timezone.utc)
    page_n = seen = inserted = 0
    last_delta = None
    while delta_url:
        data = graph_get(delta_url, fresh_token())
        msgs = data.get("value", [])
        next_url = data.get("@odata.nextLink")
        if data.get("@odata.deltaLink"):
            last_delta = data["@odata.deltaLink"]

        rows = []
        for m in msgs:
            if "id" not in m:
                continue
            seen += 1
            rows.append(to_row(m, MAILBOX, folder_map, ingested_at))

        for i in range(0, len(rows), 500):
            inserted += insert_batch(bq, rows[i:i+500])

        page_n += 1
        if page_n % 5 == 0:
            print(f"    '{folder_name}' page {page_n}: seen {seen:,}, inserted {inserted:,}", file=sys.stderr)

        if limit_remaining is not None and inserted >= limit_remaining:
            print(f"    hit --limit cap inside '{folder_name}'", file=sys.stderr)
            break

        delta_url = next_url or last_delta
        if not next_url and last_delta:
            break

    if last_delta:
        save_delta(bq, folder_key, last_delta, inserted)
    return seen, inserted


def run(limit=None, reset=False):
    bq = bigquery.Client(project=PROJECT)
    if reset:
        reset_state(bq)

    # Token refresh helper
    token_state = [time.time(), get_token()]
    def fresh_token():
        if time.time() - token_state[0] > 45 * 60:
            token_state[1] = get_token()
            token_state[0] = time.time()
        return token_state[1]

    # Folder map; skip the blacklisted ones
    print("Enumerating folders...", file=sys.stderr)
    folder_map = list_folders(MAILBOX, fresh_token())
    walk_list = [(fid, name) for fid, name in folder_map.items()
                 if name not in SKIP_FOLDERS]
    walk_list.sort(key=lambda x: x[1].lower())
    print(f"  {len(folder_map)} folders total, walking {len(walk_list)} (skipping {len(folder_map)-len(walk_list)})",
          file=sys.stderr)

    started = time.time()
    grand_seen = grand_inserted = 0
    for fid, name in walk_list:
        print(f"\n=== {name} ===", file=sys.stderr)
        remaining = (limit - grand_inserted) if limit else None
        if limit and remaining <= 0:
            print(f"  --limit reached", file=sys.stderr)
            break
        seen, inserted = walk_folder(bq, fid, name, folder_map, fresh_token, remaining)
        grand_seen += seen
        grand_inserted += inserted
        print(f"  '{name}' done: {inserted:,} inserted", file=sys.stderr)

    print(f"\n=== ALL DONE ===", file=sys.stderr)
    print(f"  folders walked : {len(walk_list)}", file=sys.stderr)
    print(f"  total seen     : {grand_seen:,}", file=sys.stderr)
    print(f"  total inserted : {grand_inserted:,}", file=sys.stderr)
    print(f"  elapsed        : {(time.time()-started)/60:.1f} min", file=sys.stderr)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="cap insertions for testing")
    p.add_argument("--reset", action="store_true",
                   help="forget any saved deltaLink and start fresh")
    args = p.parse_args()
    run(limit=args.limit, reset=args.reset)
