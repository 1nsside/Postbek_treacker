# CPA Tracker v0.4.0-postgres

PostgreSQL version of the CPA Tracker. Keeps the v0.4 attribution fields (Google/Meta/Microsoft/TikTok IDs), postback matching, spend CSV import, spend deletion, admin dashboard and reports.

## Railway variables

Required:
- `DATABASE_URL` — Railway Reference to the PostgreSQL service's `DATABASE_URL`
- `SLON_OFFER_URL`
- `ADMIN_TOKEN`
- `POSTBACK_SECRET`

`DEBUG_CLICK_IDS` must stay disabled/false in production.

`DB_PATH` is no longer used for live storage.

## Deploy

1. Replace the repository's `app/main.py` with the supplied `app/main.py`.
2. Add/replace `requirements.txt` with the supplied file so `psycopg[binary]` is installed.
3. Keep the existing Railway start command/service configuration.
4. Keep the existing `DATABASE_URL` Railway Reference; do not paste the database password manually.
5. Deploy and open `/health`.

Expected health response contains:
`"ok": true` and version `"0.4.0-postgres"`.

## Database initialization

On first startup PostgreSQL tables and indexes are created automatically:
`clicks`, `conversions`, `spend`, `offers`, `migration_state`.

If the existing `/data/cpa_tracker.db` is present on the Railway volume, the app performs a one-time best-effort import into PostgreSQL and records a migration marker so it is not repeated.

PostgreSQL becomes the live database after deployment.
