import os, sqlite3, time, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dateutil import parser

TZ=ZoneInfo("Australia/Sydney")
DB=os.getenv("DATA_DIR","/data")+"/events.db"
BOT=os.environ["TELEGRAM_BOT_TOKEN"]
CHAT=os.environ["TELEGRAM_CHAT_ID"]
POLL=int(os.getenv("POLL_SECONDS","60"))
TE_KEY=os.getenv("TRADINGECONOMICS_KEY","guest:guest")
os.makedirs(os.path.dirname(DB),exist_ok=True)
db=sqlite3.connect(DB,check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS sent (event_id TEXT PRIMARY KEY, sent_at TEXT)")
db.commit()

def send(text):
    r=requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                    json={"chat_id":CHAT,"text":text,"parse_mode":"HTML"},timeout=20)
    r.raise_for_status()

def get_calendar(start,end):
    # The dated guest endpoint now returns 410. Use the supported country
    # snapshot endpoint for guest access; paid keys may still use date filters.
    base="https://api.tradingeconomics.com/calendar/country/United%20States"
    urls=[f"{base}?c={TE_KEY}"]
    if TE_KEY != "guest:guest":
        urls.insert(0,f"{base}/{start:%Y-%m-%d}/{end:%Y-%m-%d}?c={TE_KEY}")
    last=None
    for url in urls:
        try:
            r=requests.get(url,timeout=30)
            if r.status_code == 410:
                last=r
                continue
            r.raise_for_status()
            data=r.json()
            return data if isinstance(data,list) else []
        except requests.RequestException:
            last=r if 'r' in locals() else None
            continue
    if last is not None:
        last.raise_for_status()
    return []

def event_time(e):
    raw=e.get("Date") or e.get("date")
    if not raw: return None
    dt=parser.parse(raw)
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)

def important(e):
    impact=str(e.get("Importance") or e.get("importance") or "").lower()
    country=str(e.get("Country") or e.get("country") or "").lower()
    return country in ("united states","united states of america","usa","us") and impact in ("3","high","3.0")

def fmt(v): return "N/A" if v in (None,"") else str(v)

def bias(e):
    a,b=e.get("Actual"),e.get("Forecast")
    try:
        da=float(a); db=float(b)
        return "高于预期：通常偏鹰派，短线可能利空BTC/ETH、利多美元/收益率（具体取决于指标）" if da>db else "低于预期：通常偏鸽派，短线可能利多BTC/ETH、利空美元/收益率（具体取决于指标）" if da<db else "符合预期：通常为中性"
    except: return "暂无法判断，需结合指标性质和市场即时反应"

def weekly():
    now=datetime.now(TZ); end=now+timedelta(days=7)
    ev=[e for e in get_calendar(now,end) if important(e) and event_time(e) and event_time(e)>=now]
    ev.sort(key=event_time)
    lines=["<b>下周重要宏观事件（悉尼时间）</b>"]
    for e in ev[:30]:
        lines.append(f"• {event_time(e):%a %d %b %H:%M} — {html.escape(str(e.get('Event') or e.get('event','')))}")
    send("\n".join(lines) if len(lines)>1 else "下周暂未发现符合筛选条件的美国高影响宏观事件。")

def check():
    now=datetime.now(TZ); ev=get_calendar(now-timedelta(hours=2),now+timedelta(hours=1))
    for e in ev:
        if not important(e): continue
        t=event_time(e); actual=e.get("Actual")
        if not t or t>now or actual in (None,""): continue
        eid=str(e.get("CalendarId") or e.get("ID") or f"{e.get('Event')}-{t.isoformat()}")
        if db.execute("SELECT 1 FROM sent WHERE event_id=?",(eid,)).fetchone(): continue
        name=html.escape(str(e.get("Event") or e.get("event","")))
        msg=(f"<b>宏观数据快讯</b>\n{name}\n公布时间：{t:%Y-%m-%d %H:%M} 悉尼时间\n"
             f"实际值：<b>{fmt(actual)}</b>\n预期：{fmt(e.get('Forecast'))}\n前值：{fmt(e.get('Previous'))}\n"
             f"偏差：{bias(e)}\n\n这是基于数据方向的初步判断；BTC/ETH即时反应仍需结合美元和美债收益率。")
        send(msg); db.execute("INSERT INTO sent VALUES (?,?)",(eid,now.isoformat())); db.commit()

last_weekly=None
while True:
    now=datetime.now(TZ)
    if now.weekday()==6 and now.hour==19 and now.minute<2 and last_weekly!=now.date():
        try: weekly(); last_weekly=now.date()
        except Exception as ex: print("weekly error",repr(ex),flush=True)
    try: check()
    except Exception as ex: print("poll error",repr(ex),flush=True)
    time.sleep(POLL)
