# CPA Tracker v0.3.1

FastAPI + SQLite CPA tracker with persistent Railway volume.

## v0.3.1
- Browser admin dashboard at `/admin`.
- ADMIN_TOKEN is entered via POST login and converted to an HttpOnly session cookie; it is not placed in the dashboard URL.
- Dashboard: clicks, conversions, approved, CR, revenue, spend, profit, ROI.
- Grouped reports by campaign, ad group, creative and keyword.
- Read-only recent clicks and conversions tables.
- Existing `/admin/*` JSON endpoints remain compatible with `x-admin-token` and `?token=` for API use.
- Existing Postback, spend import, attribution and persistent SQLite Volume are preserved.

## Railway variables
- `DB_PATH=/data/cpa_tracker.db`
- `SLON_OFFER_URL=...`
- `ADMIN_TOKEN=<your secret>`
- `POSTBACK_SECRET=<your secret>`
- `DEBUG_CLICK_IDS=false` (or remove it)
