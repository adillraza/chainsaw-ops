# chainsaw-ops

Internal Flask operations dashboard for Chainsaw Spares staff. Manages purchase orders, compares pricing between Neto and Retail Express (REX), item/PO-level notes, and user access control.

- **Production:** `https://ops.jonoandjohno.com.au` (Ubuntu VPS at `170.64.179.76`, nginx + systemd, port 5001 internal, HTTPS via Let's Encrypt)
- **Auto-deploy:** push to `main` → GitHub webhook pulls and restarts service (~10–20s). See `DEPLOY.md`.

## Stack

- **Python 3.11 + Flask** (app factory pattern in `app/__init__.py`), **SQLAlchemy**, **Flask-Login**.
- **SQLite** at `instance/users.db` for users/roles/annotations (backup at `instance/users.db.backup_*`).
- **BigQuery** as the analytical warehouse (service-account JSON referenced by `GOOGLE_APPLICATION_CREDENTIALS`).
- **Bootstrap 5 + vanilla JS** frontend via Jinja templates in `app/templates/`.

## Layout

```
app.py                        thin entry point — delegates to app.create_app()
config.py                     config loader
app/
  __init__.py                 create_app() factory + bootstrap_database()
  extensions.py               db, login_manager, etc.
  template_filters.py
  auth/                       login/session glue
  blueprints/
    admin/                    user/role management
    annotations/              item- and PO-level notes
    auth/                     login/logout routes
    dashboard/                overview + quick actions
    legacy_api/               older JSON endpoints (kept for compatibility)
    purchase_orders/          REX PO listing + search
    system_api/               internal API (cache control, sync)
    validation/               cost-price-check + disparity detection
  models/                     user, role, purchase_orders, annotations, reviews
  services/
    cache.py                  in-memory cache over BQ results (Refresh Data button clears it)
    msl_service.py            MSL (master stock list?) logic — see docs/bq_msl_change_decisions.sql
    purchase_orders_service.py
    reviews_sync.py
    startup.py                startup hooks
    sync_state.py
  templates/                  Jinja
  utils/

instance/                     SQLite DB (gitignored normally, but present here)
docs/                         design SQL, e.g. bq_msl_change_decisions.sql
deploy.sh                     pulled-and-run on the server
run.sh                        local dev runner
```

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env_template.txt .env       # then edit FLASK_SECRET_KEY, GOOGLE_APPLICATION_CREDENTIALS, etc.
bash run.sh                    # serves on http://localhost:5001
```

First boot: create an admin via Flask shell or by editing `instance/users.db` directly.

## Data

- Reads BigQuery Neto + REX datasets. Cost-price check compares per-SKU cost across both systems.
- Cache layer in `app/services/cache.py` — busts via the UI "Refresh Data" button or by restarting the systemd unit.

## Gotchas

- `bigquery-credentials.json` is committed at the repo root (visible in the tree). Treat it as a leaked secret — rotate if the repo has ever been public, and move to Secret Manager or env-only loading.
- `.env` file present at top level — confirm it's gitignored before commits. `env_template.txt` is the template.
- `flask_output.log` (138KB) is checked in at the root — likely a leftover; safe to delete and gitignore.
- `__pycache__/` appears at repo root (not just under `app/`) — something ran `python app.py` from the root, leaving caches. Harmless but messy.
- Prod runs as `root` per the systemd unit in README — keep that in mind for file permissions.
- SQLite is only for users/auth/notes. All the "real" operational data lives in BigQuery.
