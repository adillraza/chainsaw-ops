# Email pipeline — sales@jonoandjohno.com.au → BigQuery

## What it does

Pulls every message from `sales@jonoandjohno.com.au` (Inbox + 60-something
agent-curated folders) into `chainsawspares-385722.email_archive.messages`
in BigQuery. Drives the Customer 360 email panel and the KB feed.

## Folders

The agent curates sales@ heavily — new mail gets filed away from Inbox
within hours. The script walks **every** folder and tags each message
with `parent_folder_name`. Skipped folders (configured in
`email_backfill.SKIP_FOLDERS`):

- Drafts, Outbox, Junk Email, Deleted Items
- Conversation History, RSS Feeds, Sync Issues
- Voice Mails (audio attachments, not text)
- Recoverable Items

## Direction

Inferred from the from-address:

- `from_address == sales@jonoandjohno.com.au` → `outbound`
- otherwise → `inbound`

Includes the `Sent Items` folder (outbound replies) and `NETO invoices etc`
(automated outbound from Neto). Auto-emails are tagged `is_automated=true`
so the UI can group/demote them but the data is preserved for KB context.

## Auth

Microsoft Graph application permissions on the existing
`chainsaw-ops-sharepoint-reader` Azure AD app
(client ID `30ee98d1-7ccc-4315-a1f4-01ce96229962`). `Mail.Read` was
added 2026-05-06. Secrets in GCP Secret Manager:

- `sharepoint-tenant-id`
- `sharepoint-client-id`
- `sharepoint-client-secret`

## Three scripts

| Script | When | What |
|---|---|---|
| `scripts/email_backfill.py` | once, ~11 hours | Walks every folder's `/messages/delta`, inserts into BQ, saves a deltaLink per folder in `email_archive.sync_state` |
| `scripts/email_sync.py` | hourly via systemd | Same code, no `--reset` — every folder resumes from its saved deltaLink and only fetches what's changed |
| `scripts/email_pull_recent.py` | ad-hoc | Query BQ for one customer's email history. `--live` also tops up from Graph for the freshest possible view (mid-call refresh) |

## Schema

```sql
CREATE TABLE email_archive.messages (
  message_id            STRING,           -- Graph immutable id (PK)
  conversation_id       STRING,           -- thread grouping
  internet_message_id   STRING,           -- RFC822 ID
  mailbox               STRING,           -- 'sales@jonoandjohno.com.au'
  parent_folder_id      STRING,           -- Graph folder id
  parent_folder_name    STRING,           -- e.g. 'Customer Correspondence ONLY'
  direction             STRING,           -- 'inbound' / 'outbound'
  subject               STRING,
  from_address          STRING,
  from_name             STRING,
  to_addresses          ARRAY<STRING>,
  cc_addresses          ARRAY<STRING>,
  bcc_addresses         ARRAY<STRING>,
  received_at           TIMESTAMP,
  sent_at               TIMESTAMP,
  body_preview          STRING,           -- ~250 chars for UI
  body_text             STRING,           -- full plaintext (HTML stripped)
  body_html             STRING,           -- preserved for modal render
  has_attachments       BOOL,
  is_draft              BOOL,
  is_read               BOOL,
  is_automated          BOOL,             -- regex on subject patterns
  importance            STRING,
  web_link              STRING,           -- ⭐ click-to-Outlook URL
  ingested_at           TIMESTAMP
)
PARTITION BY DATE(received_at)
CLUSTER BY conversation_id, from_address;
```

## Volumes

Inventoried 2026-05-06:

- 257,496 messages total in sales@ (some hidden / archived not visible)
- ~165k actually visible across non-skipped folders
- ~46k in `Customer Correspondence ONLY` (highest signal)
- ~61k in `NETO invoices etc` (automated)
- ~39k in `Sent Items`
- Backfill rate ≈ 250 msg/min (delta endpoint pages cap at ~10 msgs each
  due to body size); full backfill ≈ 11 hours

## Click-to-Outlook

Every row has a `web_link` field — Microsoft Graph supplies a pre-signed
URL like `https://outlook.office.com/owa/?ItemID=AAMkA...`. Clicked from
the Customer 360 panel, opens the message in Outlook Web (or desktop
Outlook via protocol handler). SSO handles auth. No token wrangling
on our side.

## Customer 360 panel query (template)

```sql
SELECT subject, direction, received_at, web_link, body_preview,
       conversation_id, is_automated
FROM email_archive.messages
WHERE LOWER(from_address) = @customer_email
   OR @customer_email IN UNNEST(to_addresses)
ORDER BY received_at DESC LIMIT 50
```

Group by `conversation_id` for thread display, default-hide
`is_automated=true` with a "show automated" toggle.
