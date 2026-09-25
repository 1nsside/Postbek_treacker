import os
import csv
import io
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse

DB_PATH = os.getenv("DB_PATH", "/data/cpa_tracker.db")
SLON_OFFER_URL = os.getenv("SLON_OFFER_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "").strip()
DEBUG_CLICK_IDS = os.getenv("DEBUG_CLICK_IDS", "false").strip().lower() == "true"

APP_VERSION = "0.3.0"
APP_NAME = "CPA Tracker"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

APPROVED = ("approve", "approved", "схвалено")
REJECTED = ("reject", "rejected", "відхилено")
PENDING = ("pending", "очікує")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
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
        matched_click_id TEXT, raw_json TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS spend (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spent_at TEXT NOT NULL,
        source TEXT NOT NULL,
        campaign TEXT NOT NULL,
        adgroup TEXT,
        creative TEXT,
        keyword TEXT,
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'UAH',
        external_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS offers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        payout REAL,
        currency TEXT DEFAULT 'UAH',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_tx ON conversions(transaction_id) WHERE transaction_id IS NOT NULL AND transaction_id <> ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_external ON spend(external_id) WHERE external_id IS NOT NULL AND external_id <> ''")
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_clicks_created ON clicks(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_clicks_campaign ON clicks(utm_campaign)",
        "CREATE INDEX IF NOT EXISTS idx_clicks_adgroup ON clicks(utm_adgroup)",
        "CREATE INDEX IF NOT EXISTS idx_clicks_creative ON clicks(utm_creative)",
        "CREATE INDEX IF NOT EXISTS idx_clicks_term ON clicks(utm_term)",
        "CREATE INDEX IF NOT EXISTS idx_conv_status ON conversions(status)",
        "CREATE INDEX IF NOT EXISTS idx_conv_subid ON conversions(subid)",
        "CREATE INDEX IF NOT EXISTS idx_conv_click ON conversions(matched_click_id)",
        "CREATE INDEX IF NOT EXISTS idx_spend_date ON spend(spent_at)",
        "CREATE INDEX IF NOT EXISTS idx_spend_campaign ON spend(campaign)",
    ]:
        conn.execute(sql)
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
    s = str(v).strip()
    return s if s else None


def normalize_id(v):
    v = clean(v)
    return "".join(v.split()) if v else None


def parse_float(v):
    try:
        if v in (None, ""):
            return None
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_dt(v):
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(str(v).strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def add_query(url, params):
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in params.items():
        if v not in (None, ""):
            existing[k] = v
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment))


def check_admin(request: Request):
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me" or token != ADMIN_TOKEN:
        raise HTTPException(401, "Unauthorized")


def check_postback_secret(request: Request):
    if not POSTBACK_SECRET:
        return
    if request.query_params.get("pb_secret") != POSTBACK_SECRET:
        raise HTTPException(403, "Invalid postback secret")


def effective_revenue(row):
    return row["aff_rev_real"] if row["aff_rev_real"] is not None else (row["aff_rev"] or 0.0)


def status_kind(status):
    s = (clean(status) or "").lower()
    if s in APPROVED:
        return "approved"
    if s in REJECTED:
        return "rejected"
    if s in PENDING:
        return "pending"
    return "other"


def date_filter(column, start, end):
    clauses, params = [], []
    if start:
        clauses.append(f"{column} >= ?")
        params.append(start)
    if end:
        clauses.append(f"{column} < ?")
        params.append(end)
    return (" AND ".join(clauses) if clauses else "1=1"), params


@app.get("/")
def root():
    return {"service": APP_NAME, "status": "ok", "version": APP_VERSION,
            "endpoints": ["/go/slon", "/postback", "/health", "/admin/stats", "/admin/report", "/admin/spend", "/admin/campaigns"]}


@app.get("/health")
def health():
    conn = db(); conn.execute("SELECT 1").fetchone(); conn.close()
    return {"ok": True, "time": now(), "version": APP_VERSION}


@app.get("/go/slon")
async def go_slon(request: Request):
    if not SLON_OFFER_URL:
        raise HTTPException(500, "SLON_OFFER_URL is not configured")
    q = request.query_params
    click = "c_" + uuid.uuid4().hex[:20]
    row = {
        "id": click, "created_at": now(), "offer": "slon", "subid": click,
        "subid2": clean(q.get("subid2")), "subid3": clean(q.get("subid3")),
        "utm_source": clean(q.get("utm_source")), "utm_medium": clean(q.get("utm_medium")),
        "utm_campaign": clean(q.get("utm_campaign")), "utm_term": clean(q.get("utm_term")),
        "utm_adgroup": clean(q.get("utm_adgroup")), "utm_creative": clean(q.get("utm_creative")),
        "utm_device": clean(q.get("utm_device")), "utm_adposition": clean(q.get("utm_adposition")),
        "gclid": clean(q.get("gclid")), "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent")
    }
    conn = db()
    conn.execute("""INSERT INTO clicks
      (id,created_at,offer,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_creative,utm_device,utm_adposition,gclid,ip,user_agent)
      VALUES (:id,:created_at,:offer,:subid,:subid2,:subid3,:utm_source,:utm_medium,:utm_campaign,:utm_term,:utm_adgroup,:utm_creative,:utm_device,:utm_adposition,:gclid,:ip,:user_agent)""", row)
    conn.commit(); conn.close()
    if DEBUG_CLICK_IDS and q.get("show_click") == "1":
        return {"ok": True, "debug": True, "click_id": click, "subid": click, "offer": "slon"}
    target = add_query(SLON_OFFER_URL, {k: row[k] for k in ["subid","subid2","subid3","utm_source","utm_medium","utm_campaign","utm_term","utm_adgroup","utm_creative","utm_device","utm_adposition","gclid"]})
    return RedirectResponse(target, status_code=302)


@app.get("/postback")
@app.post("/postback")
async def postback(request: Request):
    check_postback_secret(request)
    data = dict(request.query_params)
    if request.method == "POST":
        if "application/json" in request.headers.get("content-type", ""):
            try:
                body = await request.json()
                if isinstance(body, dict): data.update(body)
            except Exception:
                pass
    subid = normalize_id(data.get("subid"))
    incoming_click_id = normalize_id(data.get("click_id"))
    canonical_click_id = incoming_click_id or subid
    rec = {
        "received_at": now(), "offer_id": clean(data.get("offer_id")), "offer_name": clean(data.get("offer_name")),
        "load_id": clean(data.get("load_id") or data.get("lead_id")), "transaction_id": clean(data.get("transaction_id")),
        "status": clean(data.get("lead_status")), "aff_rev": parse_float(data.get("aff_rev")),
        "aff_rev_real": parse_float(data.get("aff_rev_real")), "real_currency": clean(data.get("real_cur")),
        "currency": clean(data.get("currency")), "click_id": canonical_click_id, "subid": subid,
        "subid2": clean(data.get("subid2")), "subid3": clean(data.get("subid3")), "utm_source": clean(data.get("utm_source")),
        "utm_medium": clean(data.get("utm_medium")), "utm_campaign": clean(data.get("utm_campaign")), "utm_term": clean(data.get("utm_term")),
        "utm_adgroup": clean(data.get("utm_adgroup")), "utm_adposition": clean(data.get("utm_adposition")),
        "utm_creative": clean(data.get("utm_creative")), "utm_device": clean(data.get("utm_device")), "gclid": clean(data.get("gclid")),
        "raw_json": json.dumps(data, ensure_ascii=False)
    }
    conn = db(); matched = None
    for candidate in (subid, canonical_click_id):
        if candidate:
            matched = conn.execute("SELECT id FROM clicks WHERE id=? OR subid=? LIMIT 1", (candidate,candidate)).fetchone()
            if matched: break
    rec["matched_click_id"] = matched["id"] if matched else None
    tx = rec["transaction_id"]
    if tx:
        existing = conn.execute("SELECT id FROM conversions WHERE transaction_id=? LIMIT 1", (tx,)).fetchone()
        if existing:
            conn.close(); return {"ok": True, "duplicate": True, "conversion_id": existing["id"], "matched_click_id": rec["matched_click_id"]}
    cur = conn.execute("""INSERT INTO conversions
      (received_at,offer_id,offer_name,load_id,transaction_id,status,aff_rev,aff_rev_real,real_currency,currency,click_id,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_adposition,utm_creative,utm_device,gclid,matched_click_id,raw_json)
      VALUES (:received_at,:offer_id,:offer_name,:load_id,:transaction_id,:status,:aff_rev,:aff_rev_real,:real_currency,:currency,:click_id,:subid,:subid2,:subid3,:utm_source,:utm_medium,:utm_campaign,:utm_term,:utm_adgroup,:utm_adposition,:utm_creative,:utm_device,:gclid,:matched_click_id,:raw_json)""", rec)
    cid = cur.lastrowid; conn.commit(); conn.close()
    return {"ok": True, "duplicate": False, "conversion_id": cid, "matched_click_id": rec["matched_click_id"]}


@app.get("/admin/stats")
def stats(request: Request):
    check_admin(request); conn = db()
    clicks = conn.execute("SELECT COUNT(*) n FROM clicks").fetchone()["n"]
    conversions = conn.execute("SELECT COUNT(*) n FROM conversions").fetchone()["n"]
    approved = conn.execute("SELECT COUNT(*) n FROM conversions WHERE lower(status) IN ('approve','approved','схвалено')").fetchone()["n"]
    revenue = conn.execute("SELECT COALESCE(SUM(CASE WHEN aff_rev_real IS NOT NULL THEN aff_rev_real ELSE COALESCE(aff_rev,0) END),0) n FROM conversions WHERE lower(status) IN ('approve','approved','схвалено')").fetchone()["n"]
    matched = conn.execute("SELECT COUNT(*) n FROM conversions WHERE matched_click_id IS NOT NULL").fetchone()["n"]
    spend = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM spend WHERE currency='UAH'").fetchone()["n"]
    conn.close()
    profit = revenue - spend
    roi = (profit / spend * 100) if spend else None
    return {"version":APP_VERSION,"clicks":clicks,"conversions":conversions,"approved":approved,"matched_conversions":matched,"unmatched_conversions":conversions-matched,"revenue_uah":round(revenue,2),"spend_uah":round(spend,2),"profit_uah":round(profit,2),"roi_percent":round(roi,2) if roi is not None else None}


@app.get("/admin/clicks")
def recent_clicks(request: Request):
    check_admin(request); conn=db(); rows=conn.execute("SELECT * FROM clicks ORDER BY created_at DESC LIMIT 200").fetchall(); conn.close(); return [dict(r) for r in rows]


@app.get("/admin/conversions")
def recent_conversions(request: Request):
    check_admin(request); conn=db(); rows=conn.execute("SELECT id,received_at,offer_id,offer_name,load_id,transaction_id,status,aff_rev,aff_rev_real,real_currency,currency,click_id,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_adposition,utm_creative,utm_device,gclid,matched_click_id FROM conversions ORDER BY received_at DESC LIMIT 200").fetchall(); conn.close(); return [dict(r) for r in rows]


@app.get("/admin/spend")
def list_spend(request: Request, start: str|None=None, end: str|None=None):
    check_admin(request); conn=db(); where,params=date_filter("spent_at",start,end)
    rows=conn.execute(f"SELECT * FROM spend WHERE {where} ORDER BY spent_at DESC LIMIT 1000",params).fetchall(); conn.close(); return [dict(r) for r in rows]


@app.post("/admin/spend")
async def add_spend(request: Request):
    check_admin(request)
    data = await request.json()
    rows = data if isinstance(data,list) else [data]
    conn=db(); added=0; duplicates=0
    for r in rows:
        spent_at = parse_dt(r.get("spent_at"))
        amount = parse_float(r.get("amount"))
        source = clean(r.get("source")); campaign=clean(r.get("campaign"))
        if not spent_at or amount is None or amount < 0 or not source or not campaign:
            continue
        ext=clean(r.get("external_id"))
        if ext and conn.execute("SELECT 1 FROM spend WHERE external_id=?",(ext,)).fetchone():
            duplicates+=1; continue
        conn.execute("INSERT INTO spend(spent_at,source,campaign,adgroup,creative,keyword,amount,currency,external_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (spent_at.isoformat(),source,campaign,clean(r.get("adgroup")),clean(r.get("creative")),clean(r.get("keyword")),amount,clean(r.get("currency")) or "UAH",ext,clean(r.get("note")),now()))
        added+=1
    conn.commit(); conn.close(); return {"ok":True,"added":added,"duplicates":duplicates}


@app.post("/admin/spend/csv")
async def import_spend_csv(request: Request, file: UploadFile = File(...)):
    check_admin(request)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    required = {"spent_at","source","campaign","amount"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(400, "CSV needs: spent_at,source,campaign,amount; optional adgroup,creative,keyword,currency,external_id,note")
    conn=db(); added=0; duplicates=0; skipped=0
    for r in reader:
        spent_at=parse_dt(r.get("spent_at")); amount=parse_float(r.get("amount")); source=clean(r.get("source")); campaign=clean(r.get("campaign"))
        if not spent_at or amount is None or amount < 0 or not source or not campaign:
            skipped+=1; continue
        ext=clean(r.get("external_id"))
        if ext and conn.execute("SELECT 1 FROM spend WHERE external_id=?",(ext,)).fetchone():
            duplicates+=1; continue
        conn.execute("INSERT INTO spend(spent_at,source,campaign,adgroup,creative,keyword,amount,currency,external_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (spent_at.isoformat(),source,campaign,clean(r.get("adgroup")),clean(r.get("creative")),clean(r.get("keyword")),amount,clean(r.get("currency")) or "UAH",ext,clean(r.get("note")),now()))
        added+=1
    conn.commit(); conn.close(); return {"ok":True,"added":added,"duplicates":duplicates,"skipped":skipped}


def build_report(conn, start=None, end=None, group_fields=("campaign",)):
    click_where, click_params = date_filter("c.created_at", start, end)
    spend_where, spend_params = date_filter("s.spent_at", start, end)
    # Only UAH is aggregated into the financial report in v0.3.
    if group_fields == ("campaign",):
        group_sql = "COALESCE(NULLIF(c.utm_campaign,''),'(no_campaign)')"
        spend_group = "s.campaign"
    elif group_fields == ("campaign","adgroup"):
        group_sql = "COALESCE(NULLIF(c.utm_campaign,''),'(no_campaign)'), COALESCE(NULLIF(c.utm_adgroup,''),'(no_adgroup)')"
        spend_group = "s.campaign, COALESCE(NULLIF(s.adgroup,''),'(no_adgroup)')"
    elif group_fields == ("campaign","creative"):
        group_sql = "COALESCE(NULLIF(c.utm_campaign,''),'(no_campaign)'), COALESCE(NULLIF(c.utm_creative,''),'(no_creative)')"
        spend_group = "s.campaign, COALESCE(NULLIF(s.creative,''),'(no_creative)')"
    elif group_fields == ("campaign","keyword"):
        group_sql = "COALESCE(NULLIF(c.utm_campaign,''),'(no_campaign)'), COALESCE(NULLIF(c.utm_term,''),'(no_keyword)')"
        spend_group = "s.campaign, COALESCE(NULLIF(s.keyword,''),'(no_keyword)')"
    else:
        raise HTTPException(400,"Unsupported group")
    click_rows=conn.execute(f"SELECT {group_sql} AS g, COUNT(DISTINCT c.id) clicks, COUNT(DISTINCT CASE WHEN lower(v.status) IN ('approve','approved','схвалено') THEN v.id END) approved, COUNT(DISTINCT v.id) conversions, COALESCE(SUM(CASE WHEN lower(v.status) IN ('approve','approved','схвалено') THEN COALESCE(v.aff_rev_real,v.aff_rev,0) ELSE 0 END),0) revenue FROM clicks c LEFT JOIN conversions v ON v.matched_click_id=c.id WHERE {click_where} GROUP BY {group_sql}", click_params).fetchall()
    spend_rows=conn.execute(f"SELECT {spend_group} AS g, COALESCE(SUM(s.amount),0) spend FROM spend s WHERE s.currency='UAH' AND {spend_where} GROUP BY {spend_group}", spend_params).fetchall()
    spend_map={r["g"]:r["spend"] for r in spend_rows}
    out=[]
    for r in click_rows:
        clicks=r["clicks"]; approved=r["approved"]; conversions=r["conversions"]; revenue=float(r["revenue"] or 0); spend=float(spend_map.get(r["g"],0) or 0)
        profit=revenue-spend
        out.append({"group":r["g"],"clicks":clicks,"conversions":conversions,"approved":approved,"approval_rate_percent":round(approved/conversions*100,2) if conversions else None,"cr_click_to_approved_percent":round(approved/clicks*100,2) if clicks else None,"revenue_uah":round(revenue,2),"spend_uah":round(spend,2),"profit_uah":round(profit,2),"roi_percent":round(profit/spend*100,2) if spend else None,"epc_uah":round(revenue/clicks,4) if clicks else None,"cpa_uah":round(spend/approved,2) if approved else None})
    # Include spend-only groups so imports are visible before conversions arrive.
    existing={x["group"] for x in out}
    for g,sp in spend_map.items():
        if g not in existing:
            out.append({"group":g,"clicks":0,"conversions":0,"approved":0,"approval_rate_percent":None,"cr_click_to_approved_percent":None,"revenue_uah":0,"spend_uah":round(float(sp),2),"profit_uah":round(-float(sp),2),"roi_percent":-100.0,"epc_uah":None,"cpa_uah":None})
    out.sort(key=lambda x:(x["profit_uah"],x["revenue_uah"]), reverse=True)
    return out


@app.get("/admin/report")
def report(request: Request, start: str|None=None, end: str|None=None, group: str="campaign"):
    check_admin(request); conn=db()
    groups={"campaign":("campaign",),"adgroup":("campaign","adgroup"),"creative":("campaign","creative"),"keyword":("campaign","keyword")}
    if group not in groups: raise HTTPException(400,"group must be campaign, adgroup, creative or keyword")
    rows=build_report(conn,start,end,groups[group]); conn.close()
    totals={k:0 for k in ["clicks","conversions","approved","revenue_uah","spend_uah","profit_uah"]}
    for r in rows:
        for k in totals: totals[k]+=r[k]
    totals.update({"approval_rate_percent":round(totals["approved"]/totals["conversions"]*100,2) if totals["conversions"] else None,"cr_click_to_approved_percent":round(totals["approved"]/totals["clicks"]*100,2) if totals["clicks"] else None,"roi_percent":round(totals["profit_uah"]/totals["spend_uah"]*100,2) if totals["spend_uah"] else None,"epc_uah":round(totals["revenue_uah"]/totals["clicks"],4) if totals["clicks"] else None,"cpa_uah":round(totals["spend_uah"]/totals["approved"],2) if totals["approved"] else None})
    return {"version":APP_VERSION,"start":start,"end":end,"group":group,"totals":totals,"rows":rows}


@app.get("/admin/campaigns")
def campaigns(request: Request, start: str|None=None, end: str|None=None):
    check_admin(request); conn=db(); rows=build_report(conn,start,end,("campaign",)); conn.close(); return rows


@app.post("/admin/offers")
async def add_offer(request: Request):
    check_admin(request); data=await request.json(); oid=clean(data.get("id")); name=clean(data.get("name"))
    if not oid or not name: raise HTTPException(400,"id and name are required")
    conn=db(); conn.execute("INSERT OR REPLACE INTO offers(id,name,payout,currency,active,created_at) VALUES(?,?,?,?,?,?)",(oid,name,parse_float(data.get("payout")),clean(data.get("currency")) or "UAH",1 if data.get("active",True) else 0,now())); conn.commit(); conn.close(); return {"ok":True,"id":oid}
