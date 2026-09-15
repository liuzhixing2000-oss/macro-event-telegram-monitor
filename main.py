import hashlib
import html
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests
from dateutil import parser


SYDNEY = ZoneInfo("Australia/Sydney")
NEW_YORK = ZoneInfo("America/New_York")
DB_PATH = os.getenv("DATA_DIR", "/data") + "/events.db"
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
NEWS_POLL_SECONDS = max(180, int(os.getenv("NEWS_POLL_SECONDS", "300")))
TE_KEY = os.getenv("TRADINGECONOMICS_KEY", "guest:guest")
USER_AGENT = "macro-event-telegram-monitor/2.0"

MAJOR_FF_COUNTRIES = {"USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF"}
NON_US_KEYWORDS = (
    "interest rate", "rate decision", "monetary policy", "cpi", "inflation",
    "gdp", "employment", "unemployment", "payroll", "pmi", "retail sales",
)
WATCH_TERMS = tuple(
    x.strip().lower()
    for x in os.getenv(
        "WATCH_KEYWORDS",
        "CLARITY Act,GENIUS Act,crypto,cryptocurrency,bitcoin,ethereum,digital asset,"
        "stablecoin,SEC,CFTC,Federal Reserve,FOMC,ECB,Bank of England,Bank of Japan,"
        "People's Bank of China,interest rate,monetary policy,Treasury auction",
    ).split(",")
    if x.strip()
)

RSS_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss"),
    ("CFTC", "https://www.cftc.gov/PressRoom/PressReleases/rss"),
]
NEWS_QUERIES = [
    '"CLARITY Act" OR "GENIUS Act" OR (crypto regulation)',
    '(bitcoin OR ethereum OR stablecoin OR "digital asset") (SEC OR CFTC OR Senate OR Congress)',
    '(Federal Reserve OR FOMC OR ECB OR "Bank of England" OR "Bank of Japan") '
    '(rates OR inflation OR monetary policy)',
]

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS sent (event_id TEXT PRIMARY KEY, sent_at TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
db.commit()

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def log(message):
    print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)


def send(message):
    message = message[:4000]
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()


def get_state(key):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(key, value):
    db.execute("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)", (key, str(value)))
    db.commit()


def was_sent(event_id):
    return db.execute("SELECT 1 FROM sent WHERE event_id=?", (event_id,)).fetchone() is not None


def mark_sent(event_id):
    db.execute("INSERT OR IGNORE INTO sent(event_id,sent_at) VALUES (?,?)", (event_id, datetime.now(timezone.utc).isoformat()))
    db.commit()


def clean(value):
    if value in (None, ""):
        return "N/A"
    return html.escape(str(value))


def numeric(value):
    if value in (None, ""):
        return None
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def event_time(event):
    raw = event.get("Date") or event.get("date")
    if not raw:
        return None
    try:
        dt = parser.parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SYDNEY)
    except (TypeError, ValueError, OverflowError):
        return None


def get_calendar(start, end):
    if TE_KEY != "guest:guest":
        url = (
            "https://api.tradingeconomics.com/calendar/country/All/"
            f"{start:%Y-%m-%d}/{end:%Y-%m-%d}?c={TE_KEY}"
        )
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data:
                return data
        except (requests.RequestException, ValueError) as exc:
            log(f"TradingEconomics unavailable: {exc!r}")

    try:
        response = session.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            params={"_": int(time.time() // 60)},
            timeout=30,
        )
        response.raise_for_status()
        output = []
        for item in response.json():
            country = str(item.get("country", "")).upper()
            impact = str(item.get("impact", "")).lower()
            title = str(item.get("title", ""))
            if country not in MAJOR_FF_COUNTRIES or impact not in ("high", "3"):
                continue
            if country != "USD" and not any(k in title.lower() for k in NON_US_KEYWORDS):
                continue
            output.append({
                "CalendarId": f"ff-{item.get('date')}-{title}",
                "Date": item.get("date"),
                "Country": country,
                "Event": title,
                "Actual": item.get("actual"),
                "Forecast": item.get("forecast"),
                "Previous": item.get("previous"),
                "Importance": 3,
            })
        return output
    except (requests.RequestException, ValueError) as exc:
        log(f"calendar unavailable: {exc!r}")
        return []


def important(event):
    impact = str(event.get("Importance") or event.get("importance") or "").lower()
    return impact in ("3", "3.0", "high")


def macro_interpretation(event):
    title = str(event.get("Event") or "").lower()
    actual = numeric(event.get("Actual"))
    forecast = numeric(event.get("Forecast"))
    if actual is None or forecast is None:
        return (
            "偏差：预期值缺失或不可比较\n"
            "初步判断：中性；需要结合公布文本和即时价格反应。"
        )
    diff = actual - forecast
    if abs(diff) < 1e-12:
        return "偏差：符合预期（差值 0）\n初步判断：美元、美债收益率、BTC/ETH及美股均偏中性。"

    higher = diff > 0
    reverse = any(k in title for k in ("unemployment", "jobless claim", "claimant", "layoff"))
    hawkish = (not higher) if reverse else higher
    direction = "高于" if higher else "低于"
    unit = "个百分点" if "%" in str(event.get("Actual")) else ""
    if hawkish:
        assets = "美元偏利多；美债收益率偏上行；BTC/ETH与美股偏利空"
        meaning = "数据通常强化增长、通胀或紧缩预期"
    else:
        assets = "美元偏利空；美债收益率偏下行；BTC/ETH与美股偏利多"
        meaning = "数据通常削弱增长、通胀或紧缩预期"
    return (
        f"偏差：{direction}预期 {abs(diff):g}{unit}\n"
        f"经济含义：{meaning}\n"
        f"初步判断（基于通常市场反应的推断）：{assets}。"
    )


def send_macro(event, now):
    t = event_time(event)
    event_id = str(event.get("CalendarId") or event.get("ID") or f"{event.get('Event')}-{t.isoformat()}")
    if was_sent(event_id):
        return
    message = (
        "<b>宏观数据快讯</b>\n"
        f"事件：{clean(event.get('Event'))}（{clean(event.get('Country'))}）\n"
        f"公布时间：{t:%Y-%m-%d %H:%M} 悉尼时间\n"
        f"实际值：<b>{clean(event.get('Actual'))}</b>\n"
        f"市场预期：{clean(event.get('Forecast'))}\n"
        f"前值：{clean(event.get('Previous'))}\n"
        f"{macro_interpretation(event)}\n\n"
        "数据本身属于直接信息；资产方向属于基于通常传导的初步推断。若分项矛盾，实际反应可能相反。"
    )
    send(message)
    mark_sent(event_id)
    log(f"sent macro event {event_id}")


def check_calendar():
    now = datetime.now(SYDNEY)
    for event in get_calendar(now - timedelta(hours=3), now + timedelta(hours=1)):
        t = event_time(event)
        if not important(event) or not t or t > now or event.get("Actual") in (None, ""):
            continue
        send_macro(event, now)


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_feed(xml_text, source):
    root = ET.fromstring(xml_text)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    output = []
    for entry in entries[:40]:
        def child_text(*names):
            for name in names:
                node = entry.find(name)
                if node is not None and node.text:
                    return node.text.strip()
            return ""

        title = child_text("title", "{http://www.w3.org/2005/Atom}title")
        summary = child_text(
            "description", "summary", "content",
            "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content",
        )
        link = child_text("link")
        if not link:
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.attrib.get("href", "") if link_node is not None else ""
        guid = child_text("guid", "id", "{http://www.w3.org/2005/Atom}id")
        published = child_text(
            "pubDate", "published", "updated",
            "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated",
        )
        if title and link:
            output.append({
                "source": source,
                "title": strip_tags(title),
                "summary": strip_tags(summary),
                "link": link,
                "guid": guid or link,
                "published": published,
            })
    return output


def relevant_news(item):
    haystack = f"{item['title']} {item['summary']}".lower()
    if not any(term in haystack for term in WATCH_TERMS):
        return False
    crypto_terms = ("crypto", "bitcoin", "ethereum", "digital asset", "stablecoin", "clarity act", "genius act")
    action_terms = (
        "vote", "passes", "passed", "fails", "failed", "reject", "approve", "signs", "signed",
        "rule", "ruling", "lawsuit", "charges", "settlement", "ban", "launch", "decision",
        "raises", "raised", "cuts", "cut", "holds", "keeps", "statement", "minutes", "speech",
        "inflation", "employment", "tariff", "sanction", "auction",
    )
    if any(term in haystack for term in crypto_terms):
        return any(term in haystack for term in action_terms)
    if item["source"] in ("Federal Reserve", "Fed speeches"):
        return any(term in haystack for term in ("monetary policy", "inflation", "labor", "employment", "interest rate", "financial stability", "economy"))
    return any(term in haystack for term in action_terms)


def news_interpretation(item):
    text = f"{item['title']} {item['summary']}".lower()
    crypto = any(k in text for k in ("crypto", "bitcoin", "ethereum", "digital asset", "stablecoin", "clarity act", "genius act"))
    negative = any(k in text for k in ("fails", "failed", "rejected", "blocked", "ban", "charges", "lawsuit", "crackdown", "delay"))
    positive = any(k in text for k in ("passes", "passed", "approved", "signed", "advance", "dismissed", "clarity"))
    hawkish = any(k in text for k in ("rate hike", "raises rates", "hawkish", "inflation acceler", "higher inflation"))
    dovish = any(k in text for k in ("rate cut", "cuts rates", "dovish", "inflation slows", "lower inflation"))

    if crypto and negative:
        return "BTC/ETH与加密股短线偏利空；直接影响是监管确定性下降或合规风险上升。"
    if crypto and positive and not negative:
        return "BTC/ETH与加密股短线偏利多；直接影响是监管确定性改善。"
    if hawkish:
        return "美元和美债收益率偏利多/上行，BTC/ETH与美股偏利空。"
    if dovish:
        return "美元和美债收益率偏利空/下行，BTC/ETH与美股偏利多；若源于衰退风险，反应可能复杂。"
    return "方向暂偏中性；需要结合完整声明、美元和美债收益率的即时反应确认。"


def item_timestamp(item):
    try:
        dt = parser.parse(item.get("published") or "")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_news_items():
    items = []
    feeds = list(RSS_FEEDS)
    for query in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?q=" + quote_plus(query + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        feeds.append(("News", url))
    for source, url in feeds:
        try:
            response = session.get(url, timeout=25)
            response.raise_for_status()
            items.extend(parse_feed(response.text, source))
        except (requests.RequestException, ET.ParseError) as exc:
            log(f"feed unavailable {source}: {exc!r}")
    return items


def check_news(bootstrap=False):
    now_utc = datetime.now(timezone.utc)
    for item in fetch_news_items():
        event_id = "news-" + hashlib.sha256(item["guid"].encode("utf-8")).hexdigest()[:24]
        if was_sent(event_id):
            continue
        published = item_timestamp(item)
        recent = published is None or now_utc - timedelta(hours=8) <= published <= now_utc + timedelta(minutes=10)
        if bootstrap or not recent or not relevant_news(item):
            mark_sent(event_id)
            continue
        message = (
            "<b>政策／监管快讯</b>\n"
            f"{clean(item['title'])}\n"
            f"来源：{clean(item['source'])}\n"
            f"初步判断（基于通常市场反应的推断）：{clean(news_interpretation(item))}\n\n"
            f"<a href=\"{html.escape(item['link'], quote=True)}\">查看原文</a>"
        )
        send(message)
        mark_sent(event_id)
        log(f"sent news {item['title']}")


def weekly(calendar_cache):
    now = datetime.now(SYDNEY)
    events = [e for e in calendar_cache if important(e) and event_time(e) and event_time(e) >= now]
    events.sort(key=event_time)
    lines = ["<b>未来7天重要宏观事件（悉尼时间）</b>"]
    for event in events[:35]:
        lines.append(f"• {event_time(event):%a %d %b %H:%M} — {clean(event.get('Event'))}（{clean(event.get('Country'))}）")
    lines.append("\n央行临时讲话、国会表决与监管突发消息将由实时新闻层另行监控。")
    send("\n".join(lines))


def main():
    log("monitor v2 starting: macro calendar + central banks + crypto regulation")
    if get_state("news_bootstrap_v2") != "done":
        check_news(bootstrap=True)
        set_state("news_bootstrap_v2", "done")
        log("news feeds bootstrapped without historical alerts")

    calendar_cache = []
    cache_until = datetime.min.replace(tzinfo=SYDNEY)
    next_news_check = datetime.min.replace(tzinfo=timezone.utc)
    last_weekly = get_state("last_weekly")

    while True:
        now = datetime.now(SYDNEY)
        now_utc = datetime.now(timezone.utc)

        if now >= cache_until:
            calendar_cache = get_calendar(now, now + timedelta(days=7))
            cache_until = now + timedelta(hours=6 if calendar_cache else 1)
            log(f"calendar refreshed: {len(calendar_cache)} important events")

        if now.weekday() == 6 and now.hour == 19 and now.minute < 5 and last_weekly != now.date().isoformat():
            try:
                weekly(calendar_cache)
                last_weekly = now.date().isoformat()
                set_state("last_weekly", last_weekly)
            except Exception as exc:
                log(f"weekly error: {exc!r}")

        near_event = any(
            important(event) and event_time(event)
            and now - timedelta(hours=3) <= event_time(event) <= now + timedelta(minutes=20)
            for event in calendar_cache
        )
        if near_event:
            try:
                check_calendar()
            except Exception as exc:
                log(f"calendar poll error: {exc!r}")

        if now_utc >= next_news_check:
            try:
                check_news()
            except Exception as exc:
                log(f"news poll error: {exc!r}")
            next_news_check = now_utc + timedelta(seconds=NEWS_POLL_SECONDS)

        time.sleep(POLL_SECONDS if near_event else min(300, NEWS_POLL_SECONDS))


if __name__ == "__main__":
    main()
