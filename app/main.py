import os
import csv
import io
import json
import uuid
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SLON_OFFER_URL = os.getenv("SLON_OFFER_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
POSTBACK_SECRET = os.getenv("POSTBACK_SECRET", "").strip()
DEBUG_CLICK_IDS = os.getenv("DEBUG_CLICK_IDS", "false").strip().lower() == "true"

APP_VERSION = "0.4.0-postgres"
APP_NAME = "CPA Tracker"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

APPROVED = ("approve", "approved", "схвалено")
REJECTED = ("reject", "rejected", "відхилено")
PENDING = ("pending", "очікує")


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def init_db():
    conn = db()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            offer TEXT NOT NULL,
            subid TEXT UNIQUE NOT NULL,
            subid2 TEXT, subid3 TEXT,
            utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
            utm_term TEXT, utm_adgroup TEXT, utm_creative TEXT,
            utm_content TEXT, utm_source_platform TEXT, utm_placement TEXT,
            campaign_id TEXT, campaign_name TEXT, adgroup_id TEXT, adgroup_name TEXT,
            creative_id TEXT, creative_name TEXT,
            utm_device TEXT, utm_adposition TEXT, gclid TEXT, fbclid TEXT, msclkid TEXT, ttclid TEXT,
            ip TEXT, user_agent TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS conversions (
            id BIGSERIAL PRIMARY KEY,
            received_at TEXT NOT NULL,
            offer_id TEXT, offer_name TEXT, load_id TEXT,
            transaction_id TEXT, status TEXT,
            aff_rev DOUBLE PRECISION, aff_rev_real DOUBLE PRECISION,
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
            id BIGSERIAL PRIMARY KEY,
            spent_at TEXT NOT NULL,
            source TEXT NOT NULL,
            campaign TEXT NOT NULL,
            adgroup TEXT,
            creative TEXT,
            keyword TEXT,
            amount DOUBLE PRECISION NOT NULL,
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
            payout DOUBLE PRECISION,
            currency TEXT DEFAULT 'UAH',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS migration_state (key TEXT PRIMARY KEY, completed_at TEXT NOT NULL)")
        for sql in [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_tx ON conversions(transaction_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_external ON spend(external_id)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_created ON clicks(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_campaign ON clicks(utm_campaign)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_campaign_id ON clicks(campaign_id)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_adgroup ON clicks(utm_adgroup)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_adgroup_id ON clicks(adgroup_id)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_creative ON clicks(utm_creative)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_creative_id ON clicks(creative_id)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_term ON clicks(utm_term)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_gclid ON clicks(gclid)",
            "CREATE INDEX IF NOT EXISTS idx_clicks_fbclid ON clicks(fbclid)",
            "CREATE INDEX IF NOT EXISTS idx_conv_status ON conversions(status)",
            "CREATE INDEX IF NOT EXISTS idx_conv_subid ON conversions(subid)",
            "CREATE INDEX IF NOT EXISTS idx_conv_click ON conversions(matched_click_id)",
            "CREATE INDEX IF NOT EXISTS idx_spend_date ON spend(spent_at)",
            "CREATE INDEX IF NOT EXISTS idx_spend_campaign ON spend(campaign)",
        ]:
            conn.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_legacy_sqlite():
    # One-time best-effort migration of the existing Railway volume DB.
    # PostgreSQL remains the only live database after this import.
    legacy = os.getenv("LEGACY_SQLITE_PATH", "/data/cpa_tracker.db")
    if not os.path.exists(legacy):
        return {"migrated": False, "reason": "legacy_db_missing"}
    import sqlite3
    try:
        src = sqlite3.connect(legacy)
        src.row_factory = sqlite3.Row
    except Exception:
        return {"migrated": False, "reason": "legacy_db_unreadable"}
    pg = db()
    try:
        marker = pg.execute("SELECT 1 FROM migration_state WHERE key=%s", ("sqlite_v033_v040",)).fetchone()
        if marker:
            return {"migrated": False, "reason": "already_migrated"}
        counts = {"clicks": 0, "conversions": 0, "spend": 0, "offers": 0}
        def rows(table):
            try:
                return src.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                return []
        for r in rows("clicks"):
            d = dict(r)
            cols = ["id","created_at","offer","subid","subid2","subid3","utm_source","utm_medium","utm_campaign","utm_term","utm_adgroup","utm_creative","utm_content","utm_source_platform","utm_placement","campaign_id","campaign_name","adgroup_id","adgroup_name","creative_id","creative_name","utm_device","utm_adposition","gclid","fbclid","msclkid","ttclid","ip","user_agent"]
            vals = [d.get(c) for c in cols]
            pg.execute("""INSERT INTO clicks (id,created_at,offer,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_creative,utm_content,utm_source_platform,utm_placement,campaign_id,campaign_name,adgroup_id,adgroup_name,creative_id,creative_name,utm_device,utm_adposition,gclid,fbclid,msclkid,ttclid,ip,user_agent) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", vals)
            counts["clicks"] += 1
        for r in rows("conversions"):
            d=dict(r); cols=["received_at","offer_id","offer_name","load_id","transaction_id","status","aff_rev","aff_rev_real","real_currency","currency","click_id","subid","subid2","subid3","utm_source","utm_medium","utm_campaign","utm_term","utm_adgroup","utm_adposition","utm_creative","utm_device","gclid","matched_click_id","raw_json"]
            vals=[(d.get(c) or None) if c == "transaction_id" else d.get(c) for c in cols]
            pg.execute("""INSERT INTO conversions (received_at,offer_id,offer_name,load_id,transaction_id,status,aff_rev,aff_rev_real,real_currency,currency,click_id,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_adposition,utm_creative,utm_device,gclid,matched_click_id,raw_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (transaction_id) DO NOTHING""", vals)
            counts["conversions"] += 1
        for r in rows("spend"):
            d=dict(r); cols=["spent_at","source","campaign","adgroup","creative","keyword","amount","currency","external_id","note","created_at"]; vals=[(d.get(c) or None) if c == "external_id" else d.get(c) for c in cols]
            pg.execute("""INSERT INTO spend (spent_at,source,campaign,adgroup,creative,keyword,amount,currency,external_id,note,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (external_id) DO NOTHING""", vals)
            counts["spend"] += 1
        for r in rows("offers"):
            d=dict(r); vals=[d.get(c) for c in ["id","name","payout","currency","active","created_at"]]
            pg.execute("""INSERT INTO offers (id,name,payout,currency,active,created_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", vals)
            counts["offers"] += 1
        pg.execute("INSERT INTO migration_state(key,completed_at) VALUES(%s,%s)", ("sqlite_v033_v040", now()))
        pg.commit()
        return {"migrated": True, "counts": counts}
    except Exception:
        pg.rollback()
        raise
    finally:
        src.close(); pg.close()


@app.on_event("startup")
def startup():
    init_db()
    migrate_legacy_sqlite()


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


ADMIN_COOKIE = "cpa_admin_session"

def admin_session_value():
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me":
        return ""
    return hmac.new(ADMIN_TOKEN.encode(), b"cpa-tracker-admin-session-v1", hashlib.sha256).hexdigest()

def check_admin(request: Request):
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    cookie = request.cookies.get(ADMIN_COOKIE)
    expected_cookie = admin_session_value()
    if cookie and expected_cookie and hmac.compare_digest(cookie, expected_cookie):
        return
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
        clauses.append(f"{column} >= %s")
        params.append(start)
    if end:
        clauses.append(f"{column} < %s")
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
        "utm_content": clean(q.get("utm_content")), "utm_source_platform": clean(q.get("utm_source_platform")),
        "utm_placement": clean(q.get("utm_placement") or q.get("placement")),
        "campaign_id": clean(q.get("campaign_id") or q.get("utm_campaign_id") or q.get("campaignid")),
        "campaign_name": clean(q.get("campaign_name") or q.get("utm_campaign_name")),
        "adgroup_id": clean(q.get("adgroup_id") or q.get("utm_adgroup_id") or q.get("adgroupid")),
        "adgroup_name": clean(q.get("adgroup_name") or q.get("utm_adgroup_name")),
        "creative_id": clean(q.get("creative_id") or q.get("utm_creative_id") or q.get("creative")),
        "creative_name": clean(q.get("creative_name") or q.get("utm_creative_name")),
        "utm_device": clean(q.get("utm_device") or q.get("device")), "utm_adposition": clean(q.get("utm_adposition") or q.get("adposition")),
        "gclid": clean(q.get("gclid")), "fbclid": clean(q.get("fbclid")), "msclkid": clean(q.get("msclkid")), "ttclid": clean(q.get("ttclid")),
        "ip": request.client.host if request.client else None, "user_agent": request.headers.get("user-agent")
    }
    conn = db()
    conn.execute("""INSERT INTO clicks
      (id,created_at,offer,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_creative,utm_content,utm_source_platform,utm_placement,campaign_id,campaign_name,adgroup_id,adgroup_name,creative_id,creative_name,utm_device,utm_adposition,gclid,fbclid,msclkid,ttclid,ip,user_agent)
      VALUES (%(id)s,%(created_at)s,%(offer)s,%(subid)s,%(subid2)s,%(subid3)s,%(utm_source)s,%(utm_medium)s,%(utm_campaign)s,%(utm_term)s,%(utm_adgroup)s,%(utm_creative)s,%(utm_content)s,%(utm_source_platform)s,%(utm_placement)s,%(campaign_id)s,%(campaign_name)s,%(adgroup_id)s,%(adgroup_name)s,%(creative_id)s,%(creative_name)s,%(utm_device)s,%(utm_adposition)s,%(gclid)s,%(fbclid)s,%(msclkid)s,%(ttclid)s,%(ip)s,%(user_agent)s)""", row)
    conn.commit(); conn.close()
    if DEBUG_CLICK_IDS and q.get("show_click") == "1":
        return {"ok": True, "debug": True, "click_id": click, "subid": click, "offer": "slon"}
    forward_keys = [
        "subid","subid2","subid3","utm_source","utm_medium","utm_campaign","utm_term","utm_adgroup","utm_creative",
        "utm_content","utm_source_platform","utm_placement","campaign_id","campaign_name","adgroup_id","adgroup_name",
        "creative_id","creative_name","utm_device","utm_adposition","gclid","fbclid","msclkid","ttclid"
    ]
    target = add_query(SLON_OFFER_URL, {k: row[k] for k in forward_keys})
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
            matched = conn.execute("SELECT id FROM clicks WHERE id=%s OR subid=%s LIMIT 1", (candidate,candidate)).fetchone()
            if matched: break
    rec["matched_click_id"] = matched["id"] if matched else None
    tx = rec["transaction_id"]
    if tx:
        existing = conn.execute("SELECT id FROM conversions WHERE transaction_id=%s LIMIT 1", (tx,)).fetchone()
        if existing:
            conn.close(); return {"ok": True, "duplicate": True, "conversion_id": existing["id"], "matched_click_id": rec["matched_click_id"]}
    cur = conn.execute("""INSERT INTO conversions
      (received_at,offer_id,offer_name,load_id,transaction_id,status,aff_rev,aff_rev_real,real_currency,currency,click_id,subid,subid2,subid3,utm_source,utm_medium,utm_campaign,utm_term,utm_adgroup,utm_adposition,utm_creative,utm_device,gclid,matched_click_id,raw_json)
      VALUES (%(received_at)s,%(offer_id)s,%(offer_name)s,%(load_id)s,%(transaction_id)s,%(status)s,%(aff_rev)s,%(aff_rev_real)s,%(real_currency)s,%(currency)s,%(click_id)s,%(subid)s,%(subid2)s,%(subid3)s,%(utm_source)s,%(utm_medium)s,%(utm_campaign)s,%(utm_term)s,%(utm_adgroup)s,%(utm_adposition)s,%(utm_creative)s,%(utm_device)s,%(gclid)s,%(matched_click_id)s,%(raw_json)s) RETURNING id""", rec)
    cid = cur.fetchone()["id"]; conn.commit(); conn.close()
    return {"ok": True, "duplicate": False, "conversion_id": cid, "matched_click_id": rec["matched_click_id"]}



LOGIN_HTML = """<!doctype html><html lang=\"uk\"><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CPA Tracker — Login</title><style>body{font-family:system-ui;background:#0f1115;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}.box{width:min(420px,90vw);background:#191c23;padding:28px;border-radius:16px;box-sizing:border-box}input,button{width:100%;box-sizing:border-box;padding:13px;margin-top:10px;border-radius:10px;border:1px solid #343944;background:#0f1115;color:#fff}button{background:#fff;color:#111;font-weight:700;cursor:pointer}</style></head><body><div class=\"box\"><h2>CPA Tracker</h2><p>Адмін-доступ</p><form method=\"post\" action=\"/admin/login\"><input name=\"token\" type=\"password\" placeholder=\"ADMIN_TOKEN\" autocomplete=\"current-password\" required><button>Увійти</button></form></div></body></html>"""

ADMIN_HTML = """<!doctype html><html lang=\"uk\"><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CPA Tracker Dashboard</title><style>body{font-family:system-ui,-apple-system,sans-serif;background:#0b0d11;color:#eee;margin:0}.wrap{max-width:1200px;margin:auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.muted{color:#9aa1ad}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}.card{background:#171a21;border:1px solid #272c36;border-radius:14px;padding:14px}.label{color:#9aa1ad;font-size:12px}.value{font-size:22px;font-weight:750;margin-top:4px}section{background:#12151b;border:1px solid #272c36;border-radius:14px;padding:14px;margin-top:14px;overflow:auto}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:9px;border-bottom:1px solid #252a33;font-size:13px}th{color:#aeb5c1}button{border:1px solid #343a46;background:#1a1e27;color:#fff;padding:9px 12px;border-radius:9px;cursor:pointer}.danger{border-color:#6d3030}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}select{background:#171a21;color:#fff;border:1px solid #343a46;padding:9px;border-radius:9px}.pos{color:#76e09b}.neg{color:#ff8c8c}@media(max-width:600px){.wrap{padding:10px}.value{font-size:19px}}</style></head><body><div class=\"wrap\"><div class=\"top\"><div><h2 style=\"margin:0\">CPA Tracker</h2><div class=\"muted\">Адмін-панель · v0.4.0</div></div><div class=\"toolbar\"><button onclick=\"loadAll()\">Оновити</button><form method=\"post\" action=\"/admin/logout\"><button class=\"danger\">Вийти</button></form></div></div><div id=\"cards\" class=\"cards\"></div><section><div class=\"toolbar\"><b>Звіт</b><select id=\"group\" onchange=\"loadReport()\"><option value=\"campaign\">Campaign</option><option value=\"adgroup\">Ad Group</option><option value=\"creative\">Creative</option><option value=\"keyword\">Keyword</option></select></div><div id=\"report\" style=\"margin-top:10px\"></div></section><section><b>Останні конверсії</b><div id=\"convs\" style=\"margin-top:10px\"></div></section><section><div class=\"toolbar\"><b>Витрати</b><span class=\"muted\">CSV: spent_at, source, campaign, amount</span></div><form id=\"spendForm\" style=\"margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center\"><input id=\"spendFile\" type=\"file\" accept=\".csv,text/csv\" required style=\"max-width:100%\"><button type=\"submit\">Імпортувати CSV</button></form><div id=\"spendMsg\" class=\"muted\" style=\"margin-top:8px\"></div><div id=\"spend\" style=\"margin-top:10px\"></div></section><section><b>Останні кліки</b><div id=\"clicks\" style=\"margin-top:10px\"></div></section></div><script>const money=x=>x==null?'—':Number(x).toLocaleString('uk-UA',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₴';const pct=x=>x==null?'—':Number(x).toFixed(2)+'%';async function api(u){let r=await fetch(u,{credentials:'same-origin'});if(!r.ok){if(r.status===401)location.reload();throw new Error(await r.text())}return r.json()}function card(label,val){return `<div class=\"card\"><div class=\"label\">${label}</div><div class=\"value\">${val}</div></div>`}async function loadStats(){let d=await api('/admin/stats');let cr=d.clicks?d.approved/d.clicks*100:null;document.getElementById('cards').innerHTML=[card('Clicks',d.clicks),card('Conversions',d.conversions),card('Approved',d.approved),card('CR',pct(cr)),card('Revenue',money(d.revenue_uah)),card('Spend',money(d.spend_uah)),card('Profit',`<span class=\"${d.profit_uah>=0?'pos':'neg'}\">${money(d.profit_uah)}</span>`),card('ROI',d.roi_percent==null?'—':pct(d.roi_percent))].join('')}async function loadReport(){let g=document.getElementById('group').value;let d=await api('/admin/report?group='+encodeURIComponent(g));let h='<table><tr><th>Group</th><th>Clicks</th><th>Conv.</th><th>Approved</th><th>CR</th><th>Revenue</th><th>Spend</th><th>Profit</th><th>ROI</th><th>EPC</th><th>CPA</th></tr>';for(let r of d.rows){h+=`<tr><td>${esc(r.group)}</td><td>${r.clicks}</td><td>${r.conversions}</td><td>${r.approved}</td><td>${pct(r.cr_click_to_approved_percent)}</td><td>${money(r.revenue_uah)}</td><td>${money(r.spend_uah)}</td><td class=\"${r.profit_uah>=0?'pos':'neg'}\">${money(r.profit_uah)}</td><td>${r.roi_percent==null?'—':pct(r.roi_percent)}</td><td>${r.epc_uah==null?'—':Number(r.epc_uah).toFixed(4)+' ₴'}</td><td>${r.cpa_uah==null?'—':money(r.cpa_uah)}</td></tr>`}h+='</table>';document.getElementById('report').innerHTML=h}async function loadConv(){let a=await api('/admin/conversions');let h='<table><tr><th>Time</th><th>Offer</th><th>Status</th><th>Revenue</th><th>Campaign</th><th>Click ID</th><th>Matched</th></tr>';for(let r of a.slice(0,50)){h+=`<tr><td>${esc(r.received_at)}</td><td>${esc(r.offer_name||r.offer_id||'')}</td><td>${esc(r.status||'')}</td><td>${money(r.aff_rev_real??r.aff_rev)}</td><td>${esc(r.utm_campaign||'')}</td><td>${esc(r.click_id||'')}</td><td>${esc(r.matched_click_id||'')}</td></tr>`}document.getElementById('convs').innerHTML=h+'</table>'}async function loadClicks(){let a=await api('/admin/clicks');let h='<table><tr><th>Time</th><th>Source</th><th>Campaign ID</th><th>Campaign</th><th>Ad Group ID</th><th>Ad Group</th><th>Creative ID</th><th>Keyword</th><th>Device</th><th>Click IDs</th></tr>';for(let r of a.slice(0,50)){let ids=[r.gclid&&('G:'+r.gclid),r.fbclid&&('F:'+r.fbclid)].filter(Boolean).join(' ');h+=`<tr><td>${esc(r.created_at)}</td><td>${esc(r.utm_source||'')}</td><td>${esc(r.campaign_id||'')}</td><td>${esc(r.campaign_name||r.utm_campaign||'')}</td><td>${esc(r.adgroup_id||'')}</td><td>${esc(r.adgroup_name||r.utm_adgroup||'')}</td><td>${esc(r.creative_id||r.utm_creative||'')}</td><td>${esc(r.utm_term||'')}</td><td>${esc(r.utm_device||'')}</td><td>${esc(ids)}</td></tr>`}document.getElementById('clicks').innerHTML=h+'</table>'}async function loadSpend(){let a=await api('/admin/spend');let h='<table><tr><th>Time</th><th>Source</th><th>Campaign</th><th>Ad Group</th><th>Creative</th><th>Keyword</th><th>Amount</th><th>Currency</th><th>ID</th><th>Дія</th></tr>';for(let r of a.slice(0,50)){h+=`<tr><td>${esc(r.spent_at)}</td><td>${esc(r.source)}</td><td>${esc(r.campaign)}</td><td>${esc(r.adgroup||'')}</td><td>${esc(r.creative||'')}</td><td>${esc(r.keyword||'')}</td><td>${money(r.amount)}</td><td>${esc(r.currency||'')}</td><td>${esc(r.external_id||'')}</td><td><button class="danger" onclick="deleteSpend(${r.id})">Видалити</button></td></tr>`}document.getElementById('spend').innerHTML=h+'</table>'}async function deleteSpend(id){if(!confirm('Видалити цей запис витрат? Це змінить Spend, Profit та ROI.'))return;let r=await fetch('/admin/spend/'+id,{method:'DELETE',credentials:'same-origin'});let d;try{d=await r.json()}catch(_){d={detail:await r.text()}}if(!r.ok)throw new Error(d.detail||'Помилка видалення');document.getElementById('spendMsg').textContent=`Витрату #${id} видалено`;await loadAll()}async function importSpend(e){e.preventDefault();let f=document.getElementById('spendFile').files[0];if(!f)return;let fd=new FormData();fd.append('file',f);let r=await fetch('/admin/spend/csv',{method:'POST',body:fd,credentials:'same-origin'});let d;try{d=await r.json()}catch(_){d={detail:await r.text()}}if(!r.ok)throw new Error(d.detail||'Помилка імпорту');document.getElementById('spendMsg').textContent=`Додано: ${d.added} · Дублікати: ${d.duplicates} · Пропущено: ${d.skipped}`;document.getElementById('spendFile').value='';await loadAll()}function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}async function loadAll(){try{await Promise.all([loadStats(),loadReport(),loadConv(),loadClicks(),loadSpend()])}catch(e){document.body.insertAdjacentHTML('beforeend',`<div style=\"position:fixed;bottom:10px;left:10px;right:10px;background:#4a2020;padding:12px;border-radius:10px\">Помилка: ${esc(e.message)}</div>`)}}document.getElementById('spendForm').addEventListener('submit',importSpend);loadAll();setInterval(loadAll,60000);</script></body></html>"""

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    session = request.cookies.get(ADMIN_COOKIE)
    if session and admin_session_value() and hmac.compare_digest(session, admin_session_value()):
        return HTMLResponse(ADMIN_HTML)
    return HTMLResponse(LOGIN_HTML)

@app.post("/admin/login")
async def admin_login(request: Request):
    body = (await request.body()).decode("utf-8", errors="replace")
    from urllib.parse import parse_qs
    token = parse_qs(body).get("token", [""])[0]
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me" or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "Невірний ADMIN_TOKEN")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(ADMIN_COOKIE, admin_session_value(), httponly=True, secure=True, samesite="lax", max_age=86400)
    return response

@app.post("/admin/logout")
def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response

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
        if ext and conn.execute("SELECT 1 FROM spend WHERE external_id=%s",(ext,)).fetchone():
            duplicates+=1; continue
        conn.execute("INSERT INTO spend(spent_at,source,campaign,adgroup,creative,keyword,amount,currency,external_id,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (spent_at.isoformat(),source,campaign,clean(r.get("adgroup")),clean(r.get("creative")),clean(r.get("keyword")),amount,clean(r.get("currency")) or "UAH",ext,clean(r.get("note")),now()))
        added+=1
    conn.commit(); conn.close(); return {"ok":True,"added":added,"duplicates":duplicates}


@app.delete("/admin/spend/{spend_id}")
def delete_spend(spend_id: int, request: Request):
    check_admin(request)
    conn = db()
    row = conn.execute("SELECT id FROM spend WHERE id=%s", (spend_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Витрату не знайдено")
    conn.execute("DELETE FROM spend WHERE id=%s", (spend_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "deleted_id": spend_id}


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
        if ext and conn.execute("SELECT 1 FROM spend WHERE external_id=%s",(ext,)).fetchone():
            duplicates+=1; continue
        conn.execute("INSERT INTO spend(spent_at,source,campaign,adgroup,creative,keyword,amount,currency,external_id,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
    conn=db(); conn.execute("""INSERT INTO offers(id,name,payout,currency,active,created_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,payout=EXCLUDED.payout,currency=EXCLUDED.currency,active=EXCLUDED.active""",(oid,name,parse_float(data.get("payout")),clean(data.get("currency")) or "UAH",1 if data.get("active",True) else 0,now())); conn.commit(); conn.close(); return {"ok":True,"id":oid}
