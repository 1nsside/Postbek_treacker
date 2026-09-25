import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse

DB_PATH = os.getenv("DB_PATH", "/data/cpa_tracker.db")
SLON_OFFER_URL = os.getenv("SLON_OFFER_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")

app = FastAPI(title="CPA Tracker MVP", version="0.1.0")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        raw_json TEXT
    )
    """)
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(v):
    return None if v in (None, "") else str(v)


def add_query(url, params):
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in params.items():
        if v not in (None, ""):
            existing[k] = v
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment)
    )


@app.get("/")
def root():
    return {
        "service": "CPA Tracker MVP",
        "status": "ok",
        "version": "0.1.0",
        "endpoints": ["/go/slon", "/postback", "/health", "/admin/stats"]
    }


@app.get("/health")
def health():
    return {"ok": True, "time": now()}


@app.get("/go/slon")
async def go_slon(request: Request):
    if not SLON_OFFER_URL:
        raise HTTPException(500, "SLON_OFFER_URL is not configured")

    q = dict(request.query_params)
    click = "c_" + uuid.uuid4().hex[:20]

    row = {
        "id": click, "created_at": now(), "offer": "slon", "subid": click,
        "subid2": clean(q.get("subid2")), "subid3": clean(q.get("subid3")),
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

    target = add_query(SLON_OFFER_URL, {
        "subid": click,
        "subid2": row["subid2"], "subid3": row["subid3"],
        "utm_source": row["utm_source"], "utm_medium": row["utm_medium"],
        "utm_campaign": row["utm_campaign"], "utm_term": row["utm_term"],
        "utm_adgroup": row["utm_adgroup"], "utm_creative": row["utm_creative"],
        "utm_device": row["utm_device"], "utm_adposition": row["utm_adposition"],
        "gclid": row["gclid"],
    })
    return RedirectResponse(target, status_code=302)


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.get("/postback")
@app.post("/postback")
async def postback(request: Request):
    data = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:
            pass

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
        "click_id": clean(data.get("click_id")),
        "subid": clean(data.get("subid")),
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
    conn.execute("""
      INSERT INTO conversions
      (received_at, offer_id, offer_name, load_id, transaction_id, status,
       aff_rev, aff_rev_real, real_currency, currency, click_id, subid, subid2,
       subid3, utm_source, utm_medium, utm_campaign, utm_term, utm_adgroup,
       utm_adposition, utm_creative, utm_device, gclid, raw_json)
      VALUES
      (:received_at, :offer_id, :offer_name, :load_id, :transaction_id, :status,
       :aff_rev, :aff_rev_real, :real_currency, :currency, :click_id, :subid,
       :subid2, :subid3, :utm_source, :utm_medium, :utm_campaign, :utm_term,
       :utm_adgroup, :utm_adposition, :utm_creative, :utm_device, :gclid,
       :raw_json)
    """, rec)
    conn.commit()
    conn.close()
    return {"ok": True}


def check_admin(request: Request):
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "Unauthorized")


@app.get("/admin/stats")
def stats(request: Request):
    check_admin(request)
    conn = db()
    clicks = conn.execute("SELECT COUNT(*) n FROM clicks").fetchone()["n"]
    conversions = conn.execute("SELECT COUNT(*) n FROM conversions").fetchone()["n"]
    approved = conn.execute(
        "SELECT COUNT(*) n FROM conversions WHERE lower(status) IN ('approve','approved')"
    ).fetchone()["n"]
    revenue = conn.execute(
        "SELECT COALESCE(SUM(aff_rev_real),0) n FROM conversions"
    ).fetchone()["n"]
    conn.close()
    return {"clicks": clicks, "conversions": conversions,
            "approved": approved, "revenue": revenue}


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
    rows = conn.execute(
        "SELECT * FROM conversions ORDER BY received_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
