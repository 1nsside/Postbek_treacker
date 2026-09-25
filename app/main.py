import os
import json
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse

DB_PATH = os.getenv("DB_PATH", "/data/cpa_tracker.db")
SLON_OFFER_URL = os.getenv("SLON_OFFER_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "").strip()
DEBUG_CLICK_IDS = os.getenv("DEBUG_CLICK_IDS", "false").strip().lower() == "true"

APP_VERSION = "0.2.1"

app = FastAPI(title="CPA Tracker", version=APP_VERSION)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = db()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS clicks (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        offer TEXT NOT NULL,
        subid TEXT UNIQUE NOT NULL,
        subid2 TEXT, subid3 TEXT,
        utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
        utm_term TEXT, utm_adgroup TEXT, utm_creative TEXT,
        utm_device TEXT, utm_adposition TEXT, gclid TEXT,
        ip TEXT, user_agent TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS conversions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        offer_id TEXT, offer_name TEXT, load_id TEXT,
        transaction_id TEXT, status TEXT,
        aff_rev REAL, aff_rev_real REAL,
        real_currency TEXT, currency TEXT,
        click_id TEXT, subid TEXT, subid2 TEXT, subid3 TEXT,
        utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
        utm_term TEXT, utm_adgroup TEXT, utm_adposition TEXT,
        utm_creative TEXT, utm_device TEXT, gclid TEXT,
        matched_click_id TEXT,
        raw_json TEXT
    )
    """)

    # Safe migration for databases created by v0.1.0.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversions)").fetchall()}
    if "matched_click_id" not in cols:
        conn.execute("ALTER TABLE conversions ADD COLUMN matched_click_id TEXT")

    # Prevent exact duplicate callbacks where possible.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_tx
        ON conversions(transaction_id)
        WHERE transaction_id IS NOT NULL AND transaction_id <> ''
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_clicks_created ON clicks(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clicks_campaign ON clicks(utm_campaign)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_status ON conversions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_subid ON conversions(subid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_click ON conversions(matched_click_id)")

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(v):
    if v is None:
        return None
    value = str(v).strip()
    return value if value else None


def normalize_id(v):
    """Normalize tracking IDs; fixes accidental spaces/newlines without changing valid IDs."""
    value = clean(v)
    if not value:
        return None
    return "".join(value.split())


def parse_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def add_query(url, params):
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in params.items():
        if v not in (None, ""):
            existing[k] = v
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment)
    )


def check_admin(request: Request):
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me" or token != ADMIN_TOKEN:
        raise HTTPException(401, "Unauthorized")


def check_postback_secret(request: Request):
    if not POSTBACK_SECRET:
        return
    supplied = request.query_params.get("pb_secret")
    if supplied != POSTBACK_SECRET:
        raise HTTPException(403, "Invalid postback secret")


def effective_revenue(row):
    # aff_rev_real is preferred when supplied; otherwise use aff_rev.
    return row["aff_rev_real"] if row["aff_rev_real"] is not None else (row["aff_rev"] or 0.0)


@app.get("/")
def root():
    return {
        "service": "CPA Tracker",
        "status": "ok",
        "version": APP_VERSION,
        "endpoints": [
            "/go/slon",
            "/postback",
            "/health",
            "/admin/stats",
            "/admin/clicks",
            "/admin/conversions",
        ],
    }


@app.get("/health")
def health():
    conn = db()
    conn.execute("SELECT 1").fetchone()
    conn.close()
    return {"ok": True, "time": now(), "version": APP_VERSION}


@app.get("/go/slon")
async def go_slon(request: Request):
    if not SLON_OFFER_URL:
        raise HTTPException(500, "SLON_OFFER_URL is not configured")

    q = request.query_params
    click = "c_" + uuid.uuid4().hex[:20]

    row = {
        "id": click,
        "created_at": now(),
        "offer": "slon",
        "subid": click,
        "subid2": clean(q.get("subid2")),
        "subid3": clean(q.get("subid3")),
        "utm_source": clean(q.get("utm_source")),
        "utm_medium": clean(q.get("utm_medium")),
        "utm_campaign": clean(q.get("utm_campaign")),
        "utm_term": clean(q.get("utm_term")),
        "utm_adgroup": clean(q.get("utm_adgroup")),
        "utm_creative": clean(q.get("utm_creative")),
        "utm_device": clean(q.get("utm_device")),
        "utm_adposition": clean(q.get("utm_adposition")),
        "gclid": clean(q.get("gclid")),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }

    conn = db()
    conn.execute("""
        INSERT INTO clicks
        (id, created_at, offer, subid, subid2, subid3, utm_source,
         utm_medium, utm_campaign, utm_term, utm_adgroup, utm_creative,
         utm_device, utm_adposition, gclid, ip, user_agent)
        VALUES
        (:id, :created_at, :offer, :subid, :subid2, :subid3, :utm_source,
         :utm_medium, :utm_campaign, :utm_term, :utm_adgroup, :utm_creative,
         :utm_device, :utm_adposition, :gclid, :ip, :user_agent)
    """, row)
    conn.commit()
    conn.close()

    # Temporary, explicitly gated debug mode for attribution testing.
    # Never expose click IDs publicly unless DEBUG_CLICK_IDS=true is enabled.
    if DEBUG_CLICK_IDS and q.get("show_click") == "1":
        return {
            "ok": True,
            "debug": True,
            "click_id": click,
            "subid": click,
            "offer": "slon",
            "next": "Open /go/slon normally after this test; this response intentionally does not redirect."
        }

    target = add_query(SLON_OFFER_URL, {
        "subid": click,
        "subid2": row["subid2"],
        "subid3": row["subid3"],
        "utm_source": row["utm_source"],
        "utm_medium": row["utm_medium"],
        "utm_campaign": row["utm_campaign"],
        "utm_term": row["utm_term"],
        "utm_adgroup": row["utm_adgroup"],
        "utm_creative": row["utm_creative"],
        "utm_device": row["utm_device"],
        "utm_adposition": row["utm_adposition"],
        "gclid": row["gclid"],
    })
    return RedirectResponse(target, status_code=302)


@app.get("/postback")
@app.post("/postback")
async def postback(request: Request):
    check_postback_secret(request)

    data = dict(request.query_params)
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    data.update(body)
            except Exception:
                pass

    # PDL-Profit currently returns {subid}; our subid is the canonical click ID.
    subid = normalize_id(data.get("subid"))
    incoming_click_id = normalize_id(data.get("click_id"))
    canonical_click_id = incoming_click_id or subid

    rec = {
        "received_at": now(),
        "offer_id": clean(data.get("offer_id")),
        "offer_name": clean(data.get("offer_name")),
        "load_id": clean(data.get("load_id")),
        "transaction_id": clean(data.get("transaction_id")),
        "status": clean(data.get("lead_status")),
        "aff_rev": parse_float(data.get("aff_rev")),
        "aff_rev_real": parse_float(data.get("aff_rev_real")),
        "real_currency": clean(data.get("real_cur")),
        "currency": clean(data.get("currency")),
        "click_id": canonical_click_id,
        "subid": subid,
        "subid2": clean(data.get("subid2")),
        "subid3": clean(data.get("subid3")),
        "utm_source": clean(data.get("utm_source")),
        "utm_medium": clean(data.get("utm_medium")),
        "utm_campaign": clean(data.get("utm_campaign")),
        "utm_term": clean(data.get("utm_term")),
        "utm_adgroup": clean(data.get("utm_adgroup")),
        "utm_adposition": clean(data.get("utm_adposition")),
        "utm_creative": clean(data.get("utm_creative")),
        "utm_device": clean(data.get("utm_device")),
        "gclid": clean(data.get("gclid")),
        "raw_json": json.dumps(data, ensure_ascii=False),
    }

    conn = db()

    # Match conversion to the original click. Prefer subid, then click_id.
    matched = None
    for candidate in (subid, canonical_click_id):
        if candidate:
            matched = conn.execute(
                "SELECT id FROM clicks WHERE id = ? OR subid = ? LIMIT 1",
                (candidate, candidate),
            ).fetchone()
            if matched:
                break

    rec["matched_click_id"] = matched["id"] if matched else None

    # Idempotency: repeated callback with the same transaction_id is acknowledged
    # but not inserted a second time.
    tx = rec["transaction_id"]
    if tx:
        existing = conn.execute(
            "SELECT id FROM conversions WHERE transaction_id = ? LIMIT 1", (tx,)
        ).fetchone()
        if existing:
            conn.close()
            return {
                "ok": True,
                "duplicate": True,
                "conversion_id": existing["id"],
                "matched_click_id": rec["matched_click_id"],
            }

    cur = conn.execute("""
      INSERT INTO conversions
      (received_at, offer_id, offer_name, load_id, transaction_id, status,
       aff_rev, aff_rev_real, real_currency, currency, click_id, subid, subid2,
       subid3, utm_source, utm_medium, utm_campaign, utm_term, utm_adgroup,
       utm_adposition, utm_creative, utm_device, gclid, matched_click_id,
       raw_json)
      VALUES
      (:received_at, :offer_id, :offer_name, :load_id, :transaction_id, :status,
       :aff_rev, :aff_rev_real, :real_currency, :currency, :click_id, :subid,
       :subid2, :subid3, :utm_source, :utm_medium, :utm_campaign, :utm_term,
       :utm_adgroup, :utm_adposition, :utm_creative, :utm_device, :gclid,
       :matched_click_id, :raw_json)
    """, rec)
    conversion_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "duplicate": False,
        "conversion_id": conversion_id,
        "matched_click_id": rec["matched_click_id"],
    }


@app.get("/admin/stats")
def stats(request: Request):
    check_admin(request)
    conn = db()

    clicks = conn.execute("SELECT COUNT(*) n FROM clicks").fetchone()["n"]
    conversions = conn.execute("SELECT COUNT(*) n FROM conversions").fetchone()["n"]
    approved = conn.execute("""
        SELECT COUNT(*) n FROM conversions
        WHERE lower(status) IN ('approve', 'approved', 'схвалено')
    """).fetchone()["n"]

    revenue = conn.execute("""
        SELECT COALESCE(SUM(
            CASE
                WHEN aff_rev_real IS NOT NULL THEN aff_rev_real
                WHEN aff_rev IS NOT NULL THEN aff_rev
                ELSE 0
            END
        ), 0) n
        FROM conversions
        WHERE lower(status) IN ('approve', 'approved', 'схвалено')
    """).fetchone()["n"]

    matched = conn.execute("""
        SELECT COUNT(*) n FROM conversions
        WHERE matched_click_id IS NOT NULL
    """).fetchone()["n"]

    conn.close()

    return {
        "version": APP_VERSION,
        "clicks": clicks,
        "conversions": conversions,
        "approved": approved,
        "matched_conversions": matched,
        "unmatched_conversions": conversions - matched,
        "revenue": revenue,
    }


@app.get("/admin/clicks")
def recent_clicks(request: Request):
    check_admin(request)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM clicks ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/admin/conversions")
def recent_conversions(request: Request):
    check_admin(request)
    conn = db()
    rows = conn.execute("""
        SELECT
            id, received_at, offer_id, offer_name, load_id, transaction_id,
            status, aff_rev, aff_rev_real, real_currency, currency,
            click_id, subid, subid2, subid3,
            utm_source, utm_medium, utm_campaign, utm_term,
            utm_adgroup, utm_adposition, utm_creative, utm_device, gclid,
            matched_click_id
        FROM conversions
        ORDER BY received_at DESC
        LIMIT 100
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/admin/campaigns")
def campaign_stats(request: Request):
    """Simple campaign-level attribution view; spend/ROI comes later when spend is imported."""
    check_admin(request)
    conn = db()
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(c.utm_campaign, ''), '(no_campaign)') AS campaign,
            COUNT(DISTINCT c.id) AS clicks,
            COUNT(DISTINCT CASE
                WHEN lower(v.status) IN ('approve','approved','схвалено')
                THEN v.id END) AS approved,
            COALESCE(SUM(CASE
                WHEN lower(v.status) IN ('approve','approved','схвалено')
                THEN COALESCE(v.aff_rev_real, v.aff_rev, 0)
                ELSE 0 END), 0) AS revenue
        FROM clicks c
        LEFT JOIN conversions v
          ON v.matched_click_id = c.id
        GROUP BY COALESCE(NULLIF(c.utm_campaign, ''), '(no_campaign)')
        ORDER BY revenue DESC, clicks DESC
        LIMIT 200
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
