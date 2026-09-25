# CPA Tracker MVP

Small first-party tracker for the PDL-Profit workflow.

### Flow
Ad -> /go/slon -> PDL-Profit -> SlonCredit -> PDL-Profit postback -> /postback

### Endpoints
- GET /go/slon : creates a unique click ID, stores tracking data and redirects.
- GET/POST /postback : receives PDL-Profit conversion data.
- GET /admin/stats?token=... : basic stats.
- GET /admin/clicks?token=... : recent clicks.
- GET /admin/conversions?token=... : recent conversions.
- GET /health : health check.

### Railway variables
- SLON_OFFER_URL = your existing working PDL-Profit SlonCredit tracking URL.
- ADMIN_TOKEN = a long random secret.
- DB_PATH = /data/cpa_tracker.db when a Railway persistent volume is mounted at /data.

### PDL-Profit postback
Use:
https://YOUR-RAILWAY-DOMAIN/postback

Map the PDL-Profit placeholders shown in its UI to these query parameter names:
offer_name, offer_id, load_id, lead_status, lead_date, click_date,
transaction_id, aff_rev, aff_rev_real, real_cur, currency, lead_type,
click_id, subid, subid2, subid3, utm_source, utm_medium, utm_campaign,
utm_term, utm_adgroup, utm_adposition, utm_creative, utm_device, gclid.

Before paid traffic, test a click and then a postback event. Do not scale until attribution is verified.

### Important
This is an MVP. Before production scale, add postback idempotency/deduplication,
conversion-to-click matching, ad-spend imports, ROI dashboards, fraud filtering,
privacy/retention controls, backups, multiple offers and traffic sources.
