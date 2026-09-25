# CPA Tracker v0.2.0

FastAPI + SQLite tracker for CPA traffic.

## What v0.2 adds

- Keeps the existing `/go/slon` and `/postback` endpoints.
- Normalizes `subid` / `click_id` so accidental spaces/newlines do not break attribution.
- Uses PDL-Profit `{subid}` as the canonical click ID.
- Matches every postback to the original click (`matched_click_id`).
- Idempotency: repeated callbacks with the same `transaction_id` are not inserted twice.
- Revenue uses `aff_rev_real` when available, otherwise `aff_rev`.
- Adds `/admin/campaigns` for campaign-level clicks/approved/revenue.
- Keeps the old SQLite database compatible through a small startup migration.
- Optional `POSTBACK_SECRET` can protect the public `/postback` endpoint.

## Railway variables

Keep:

- `SLON_OFFER_URL`
- `ADMIN_TOKEN`
- `DB_PATH=/data/cpa_tracker.db`

Optional after deployment:

- `POSTBACK_SECRET=<your own long random secret>`

If `POSTBACK_SECRET` is enabled, add `&pb_secret=YOUR_SECRET` to the PDL-Profit Postback URL.

## IMPORTANT: Railway Volume

Because SQLite is stored in `/data`, attach a Railway Volume and mount it at:

`/data`

Do this before paid traffic. Otherwise a redeploy/replacement of the container can lose the SQLite database.

## PDL-Profit Postback URL

The PDL-Profit placeholders should map to the tracker like this:

`offer_name={offer_name}`
`offer_id={offer_id}`
`load_id={lead_id}`
`lead_status={lead_status}`
`lead_date={lead_date}`
`click_date={click_date}`
`transaction_id={transaction_id}`
`aff_rev={aff_rev}`
`aff_rev_real={aff_rev_real_cur}`
`real_cur={real_cur}`
`currency={currency}`
`lead_type={lead_type}`
`click_ip={click_ip}`
`click_id={subid}`
`subid={subid}`
`subid2={subid2}`
`subid3={subid3}`
`utm_source={utm_source}`
`utm_medium={utm_medium}`
`utm_campaign={utm_campaign}`
`utm_term={utm_term}`
`utm_adgroup={utm_adgroup}`
`utm_adposition={utm_adposition}`
`utm_creative={utm_creative}`
`utm_device={utm_device}`
`gclid={gclid}`

Statuses to send:

- pending
- approved
- rejected

## Admin endpoints

Use the `token` query parameter or `X-Admin-Token` header.

- `/health`
- `/admin/stats`
- `/admin/clicks`
- `/admin/conversions`
- `/admin/campaigns`

Example:

`https://YOUR-RAILWAY-DOMAIN/health`

## Test

1. Open `/go/slon?utm_source=test&utm_campaign=check_003`
2. Confirm redirect to SlonCredit.
3. Check `/admin/clicks`.
4. Send a test postback using the returned `subid`.
5. Check `/admin/conversions`.
6. Check `/admin/stats` and confirm `matched_conversions` increased.

## Not production-complete yet

Before scaling paid traffic, add:

- persistent Railway Volume;
- a strong `POSTBACK_SECRET`;
- spend import/API for each traffic source;
- ROI/profit dashboard;
- offer routing for multiple offers;
- privacy/retention controls;
- rate limiting and additional abuse protection;
- backups/restore procedure.
