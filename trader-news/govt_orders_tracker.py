#!/usr/bin/env python3
"""Government Order, Tender & Execution Risk Tracker (trader-news): who won which government or PSU
contract, how big it is against the company, and which orders are slipping, from CAPTCHA-free public
sources only, with no language model anywhere.

    python3 govt_orders_tracker.py                    # one pass over every source -> trader/orders/
    python3 govt_orders_tracker.py --watch 60         # poll every 60 s in market hours (15 min outside)
    python3 govt_orders_tracker.py --only nse,gnews   # some sources
    python3 govt_orders_tracker.py --days 3 --all     # a wider window, items already flagged too
    python3 govt_orders_tracker.py ledger             # the order ledger: open orders, lag and loss flags
    python3 govt_orders_tracker.py --dry-run --json   # fetch and parse, write nothing

Sources (each on its own minimum interval, so a 60-second loop never hammers anyone; one failing
source never stops the rest):
  nse     NSE corporate announcements, Regulation 30 (www.nseindia.in/api/corporate-announcements), 60 s
  bse     BSE announcements (the same collector as news.py; BSE's RSS when its API refuses), 60 s
  gnews   Google News RSS searches (L1 bidder, letter of award, work order, MSEDCL, railways, power,
          defence, NHAI / infra, cancellations, debarments, results), 120 s
  press   Economic Times, Moneycontrol and LiveMint RSS, 120 s
  pib     Press Information Bureau releases RSS (ministries: railways, power, defence, roads), 5 min
  gem     GeM public bid / award lists (bidplus.gem.gov.in), 30 min, best effort
  cppp    CPPP / eProcure debarment lists (eprocure.gov.in), 30 min, best effort
A page that shows a CAPTCHA or a login is skipped and said; it is never solved or worked around.
Feed URLs can be changed in trader/orders_sources.json ({"press": [...], "gnews_queries": [...]}).

What it reads from each item (regex, no AI):
  kind       award (letter of award, LoA, L1 bidder, work order, contract awarded), cancel
             (cancellation, termination, withdrawal), delay (delay in execution, liquidated damages,
             guarantee encashed), debar (debarment, blacklisting), milestone (completion,
             commissioning, COD), results (profit / revenue change in a news report)
  value      Rs crore / lakh / million / billion (USD at --usd-inr, 85), the figure next to "worth",
             "valued at", "aggregating" first; an "order book of" figure is never the order
  impact     value / market cap (NSE's quote API, or trader/market_caps.json); HIGH IMPACT at >= 5 %
  timeline   "to be executed within 24 months", "over 3 years", "by March 2028"
  client     MSEDCL / Maharashtra, Railways, Power, Defence, Roads / infra, other state governments
News headlines are matched to listed companies through NSE's EQUITY_L.csv names (whole words, generic
one-word names never count) and trader/company_aliases.json ({"RVNL": "RVNL", "L&T": "LT"}).

The ledger trader/history/orders.db (SQLite) keeps every item. Each run it flags:
  EXECUTION LAG WATCH         65 % or more of a stated timeline has passed and the company has filed no
                              milestone (completion, commissioning) since the award
  ORDER LOSS / CANCELLATION   a cancellation, termination, delay or liquidated-damages item after an
                              award (matched on the value within 25 % or the same client; otherwise
                              by symbol only, and it says so)
  DEBARMENT                   the company was debarred or blacklisted

Output: the terminal; trader/orders/latest.json (this run plus the ledger flags, in the item shape
trader-entry, exit, risk and alerts read through india_signals.py); new items appended to
trader/orders/alerts.csv; trader/state/orders.json remembers what was flagged.

News is late: a filing comes first, the press minutes to hours later. Disclosures are leads, not
signals. Nothing here places an order.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_http as ih  # noqa: E402
import india_market as im  # noqa: E402
import india_catalysts as ic  # noqa: E402

SOURCES = ("nse", "bse", "gnews", "press", "pib", "gem", "cppp")
MIN_INTERVAL = {"nse": 60, "bse": 60, "gnews": 120, "press": 120, "pib": 300, "gem": 1800, "cppp": 1800}
GNEWS = "https://news.google.com/rss/search?q=%s+when:%dd&hl=en-IN&gl=IN&ceid=IN:en"
GNEWS_QUERIES = [
    '"L1 bidder"', '"letter of award"', '"work order" crore', '"order worth" crore', '"bags order"',
    'MSEDCL order', 'Maharashtra tender L1', 'railway order crore', '"Rail Vikas" order', 'NHAI contract crore',
    'defence ministry contract crore', 'power transmission order crore', '"contract terminated" crore',
    '"order cancelled" company', 'debarred company tender', '"liquidated damages"',
    'quarterly results net profit crore', 'shareholding promoter stake crore',
]
PRESS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.livemint.com/rss/markets",
    "https://www.livemint.com/rss/companies",
]
PIB = ["https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"]
GEM = ["https://bidplus.gem.gov.in/all-bids"]
CPPP = ["https://eprocure.gov.in/cppp/debarmentlist"]
OUT_DIR = os.path.join("trader", "orders")
DB_PATH = os.path.join("trader", "history", "orders.db")
STATE = os.path.join("trader", "state", "orders.json")
FEEDS_FILE = os.path.join("trader", "orders_sources.json")
ALIASES = os.path.join("trader", "company_aliases.json")
CSV_FIELDS = ("time", "kind", "symbol", "company", "title", "detail", "numbers", "flags", "source", "link", "id")
HIGH_IMPACT = 5.0
LAG_SHARE = 0.65
VALUE_MATCH = 0.25
USD_INR = 85.0
KIND_OF_TYPE = {"order_win": "award", "order_cancel": "cancel", "execution_delay": "delay", "debarment": "debar",
                "order_milestone": "milestone"}
DIRECTION = {"award": "up", "milestone": "up", "cancel": "down", "delay": "down", "debar": "down", "results": "?"}
LABEL = {"award": "ORDER WIN", "cancel": "ORDER LOSS / CANCELLATION", "delay": "EXECUTION DELAY",
         "debar": "DEBARMENT", "milestone": "MILESTONE", "results": "RESULTS (press)"}
CAPTCHA = re.compile(r"captcha|g-recaptcha|hcaptcha|please log ?in|sign in to continue", re.I)


# ---------------------------------------------------------------- reading an item (regex only)

def classify(text, desc=""):
    """(kind, event type) for a filing or headline about contracts, or (None, None)."""
    et, _ = ic.event_type(text, desc)
    if et in KIND_OF_TYPE:
        return KIND_OF_TYPE[et], et
    if RESULTS.search(text or ""):
        return "results", "results"
    return None, None


CUR = r"(?:INR|Rs\.?|₹|Rupees)"
AMOUNT = r"(\d[\d,]*(?:\.\d+)?)"
UNITS = {"crore": 1.0, "crores": 1.0, "cr": 1.0, "crs": 1.0, "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
         "million": 0.1, "mn": 0.1, "billion": 100.0, "bn": 100.0}
VALUE_RX = re.compile(r"(%s|USD|US\$|\$)\s*%s\s*(crores?|crs?|lakhs?|lacs?|million|mn|billion|bn)?\b\.?" % (CUR, AMOUNT),
                      re.I)
LEAD = re.compile(r"(?:worth|valued at|value of|aggregating(?: to)?|amounting to|totall?ing|of|for|order of|"
                  r"contract of|cost of)\s*(?:approx(?:imately|\.)?|about|around|nearly|over|more than)?\s*$", re.I)
NOT_ORDER = re.compile(r"order book|order backlog|order inflow (?:guidance|target)|market cap|revenue|turnover|"
                       r"profit|net worth|capex|market value", re.I)


def amounts(text, usd_inr=USD_INR):
    """[(Rs crore, start, end, led by 'worth'...)] for every money figure in the text."""
    out = []
    for m in VALUE_RX.finditer(text or ""):
        cur, amt, unit = m.group(1), m.group(2), (m.group(3) or "").lower().rstrip(".")
        try:
            x = float(amt.replace(",", ""))
        except ValueError:
            continue
        usd = cur.upper() in ("USD", "US$", "$")
        if unit:
            cr = x * UNITS.get(unit, 1.0)
        elif usd:
            continue                         # "$5" with no unit is not an order value
        else:
            cr = x / 1e7                     # plain rupees: "Rs 4,52,00,000"
        if usd:
            cr *= usd_inr                    # USD 5 million = 0.5 "crore of dollars" x 85 = Rs 42.5 crore
        before = (text or "")[max(0, m.start() - 40):m.start()]
        out.append((round(cr, 2), m.start(), m.end(), bool(LEAD.search(before)), bool(NOT_ORDER.search(before))))
    return out


def order_value_cr(text, usd_inr=USD_INR):
    """The order's value in Rs crore, or None. The figure led by 'worth' / 'valued at' / 'aggregating'
    wins; a figure right after 'order book', 'revenue' or 'profit' is never the order."""
    vals = [a for a in amounts(text, usd_inr) if not a[4] and 0.01 <= a[0] < 500000]
    if not vals:
        return None
    led = [a for a in vals if a[3]]
    return (led or vals)[0][0]


TIMELINE = re.compile(r"(?:within|over|in|period of|span of|tenure of|duration of|completed in|executed in|completion "
                      r"(?:period|time|timeline)(?: of)?)\s+(?:a\s+)?(?:period\s+of\s+)?(\d+(?:\.\d+)?|one|two|three|four|five|"
                      r"six|twelve|eighteen|twenty[- ]four|thirty[- ]six)\s*(?:\([^)]{1,25}\))?\s*(months?|years?|days?)\b", re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "twelve": 12, "eighteen": 18,
         "twenty-four": 24, "twenty four": 24, "thirty-six": 36, "thirty six": 36}
BY_DATE = re.compile(r"\bby (?:the end of )?(January|February|March|April|May|June|July|August|September|October|"
                     r"November|December)[,\s]+(\d{4})\b|\bby (?:FY|F\.Y\.\s*)(\d{2,4})\b", re.I)
MONTH = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                     "september", "october", "november", "december"), 1)}


def timeline_months(text):
    """'to be executed within 24 months' -> 24.0; years and days converted; None when not stated."""
    for m in TIMELINE.finditer(text or ""):
        n = m.group(1).lower()
        x = float(WORDS.get(n, n)) if not re.match(r"^\d", n) else float(n)
        unit = m.group(2).lower()
        months = x * 12 if unit.startswith("year") else x / 30.4 if unit.startswith("day") else x
        if 0.5 <= months <= 180:
            return round(months, 1)
    return None


def due_date(text, award_day):
    """The stated completion date: 'by March 2028' / 'by FY28' -> 'YYYY-MM-DD', else award + timeline."""
    m = BY_DATE.search(text or "")
    if m and m.group(1):
        mo, yr = MONTH[m.group(1).lower()], int(m.group(2))
        nxt = _dt.date(yr + (mo == 12), mo % 12 + 1, 1)
        return (nxt - _dt.timedelta(days=1)).isoformat()
    if m and m.group(3):
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return "%d-03-31" % y                                  # an Indian fiscal year ends on 31 March
    months = timeline_months(text)
    if months and award_day:
        d = _dt.date.fromisoformat(award_day[:10])
        return (d + _dt.timedelta(days=int(round(months * 30.44)))).isoformat()
    return None


CLIENTS = [
    ("MSEDCL / Maharashtra", "state power", r"\bMSEDCL\b|Maharashtra State Electricity|MAHADISCOM|MAHATRANSCO|MAHAGENCO|"
                                            r"Mahavitaran|Maharashtra (?:government|state)|Government of Maharashtra|MMRDA|"
                                            r"\bCIDCO\b|MSRDC|Maha ?Metro"),
    ("Railways", "railways", r"\bRailways?\b|\bRVNL\b|Rail Vikas|\bRDSO\b|\bDFCCIL\b|\bIRCON\b|\bRITES\b|Metro Rail|\bNCRTC\b|"
                             r"Vande Bharat|Kavach|(?:Central|Western|Northern|Southern|Eastern|North[- ]?\w+|South[- ]?\w+|"
                             r"East Coast|West Central|East Central) Railway"),
    ("Power", "power", r"\bNTPC\b|Power ?Grid|\bPGCIL\b|\bNHPC\b|\bSECI\b|\bSJVN\b|\bDISCOM|transmission|substation|"
                       r"Ministry of Power|\bREC\b|\bPFC\b|solar|thermal power|hydro"),
    ("Defence", "defence", r"Ministry of Defence|\bMoD\b|\bDRDO\b|Indian (?:Army|Navy|Air Force)|Coast Guard|defence|"
                           r"\bBDL\b|\bHAL\b|\bBEL\b|Mazagon|Cochin Shipyard|Garden Reach"),
    ("Roads / infra", "infra", r"\bNHAI\b|\bMoRTH\b|\bNHIDCL\b|highway|expressway|Jal Jeevan|smart city|\bAAI\b|airport|"
                               r"port trust|Sagarmala|Bharatmala|\bPWD\b|irrigation"),
    ("State government", "state", r"Government of [A-Z][a-z]+|State Government|[A-Z][a-z]+ State [A-Z][a-z]+ Corporation"),
    ("PSU / central government", "central", r"Government of India|\bPSU\b|Ministry of|\bONGC\b|\bIOCL?\b|\bBPCL\b|"
                                            r"\bHPCL\b|\bGAIL\b|\bSAIL\b|\bBHEL\b|\bCoal India\b|\bNMDC\b"),
]
CLIENT_RX = [(lab, sec, re.compile(rx)) for lab, sec, rx in CLIENTS]


def client_of(text):
    for lab, sec, rx in CLIENT_RX:
        if rx.search(text or ""):
            return lab, sec
    return "", ""


RESULTS = re.compile(r"(?:net profit|\bPAT\b|profit after tax|revenue|net sales|total income)[^.]{0,60}?\b(?:rose|rises|"
                     r"jumped|jumps|surged|surges|grew|grows|up|climbed|soared|fell|falls|declined|declines|down|dropped|"
                     r"drops|plunged|plunges|slumped)\b[^.%]{0,25}?(\d+(?:\.\d+)?)\s*(?:%|per ?cent)", re.I)
DOWN_VERB = re.compile(r"\b(?:fell|falls|declined|declines|down|dropped|drops|plunged|plunges|slumped)\b", re.I)


def results_change(text):
    """{'profit_yoy': +34.0, 'revenue_yoy': -5.0} from a press line, as printed."""
    out = {}
    for m in RESULTS.finditer(text or ""):
        what = "revenue_yoy" if re.match(r"revenue|net sales|total income", m.group(0), re.I) else "profit_yoy"
        if what in out:
            continue
        x = float(m.group(1))
        out[what] = -x if DOWN_VERB.search(m.group(0)) else x
    return out


# ---------------------------------------------------------------- the ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, time TEXT, symbol TEXT, company TEXT, kind TEXT, direction TEXT, client TEXT,
  sector TEXT, value_cr REAL, mcap_cr REAL, impact_pct REAL, timeline_months REAL, due_date TEXT,
  title TEXT, detail TEXT, link TEXT, source TEXT, first_seen TEXT
);
CREATE INDEX IF NOT EXISTS items_sym ON items(symbol, time);
"""


def connect(path=DB_PATH):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def store(con, items, seen_at):
    for it in items:
        n = it["numbers"]
        con.execute("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["id"], it["time"], it["symbol"], it["company"], it["kind"], it["direction"], it["client"],
                     it["sector"], n.get("value_cr"), n.get("mcap_cr"), n.get("impact_pct"), n.get("timeline_months"),
                     n.get("due_date"), it["title"], it["detail"], it["link"], it["source"], seen_at))
    con.commit()


def _rows(con, where="", args=()):
    cols = [c[1] for c in con.execute("PRAGMA table_info(items)")]
    return [dict(zip(cols, r)) for r in con.execute("SELECT * FROM items %s ORDER BY time" % where, args)]


def ledger_flags(con, today):
    """Execution lag, order loss and debarment flags over the whole ledger: [{flag, symbol, ...}]."""
    rows = _rows(con, "WHERE symbol != ''")
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    out = []
    t = _dt.date.fromisoformat(today)
    for sym, rs in by_sym.items():
        awards = [r for r in rs if r["kind"] == "award"]
        bad = [r for r in rs if r["kind"] in ("cancel", "delay")]
        for b in bad:
            prior = [a for a in awards if (a["time"] or "") <= (b["time"] or "")]
            match, how = None, "symbol only"
            for a in reversed(prior):
                if a["value_cr"] and b["value_cr"] and abs(a["value_cr"] - b["value_cr"]) <= VALUE_MATCH * a["value_cr"]:
                    match, how = a, "value"
                    break
                if a["client"] and a["client"] == b["client"]:
                    match, how = a, "client"
                    break
            if match is None and prior:
                match = prior[-1]
            out.append({"flag": "ORDER LOSS / CANCELLATION", "symbol": sym, "time": b["time"], "title": b["title"],
                        "link": b["link"], "kind": b["kind"], "value_cr": b["value_cr"] or (match or {}).get("value_cr"),
                        "order": (match or {}).get("title"), "order_link": (match or {}).get("link"),
                        "matched_by": how if match else "no award on file"})
        for b in (r for r in rs if r["kind"] == "debar"):
            out.append({"flag": "DEBARMENT", "symbol": sym, "time": b["time"], "title": b["title"], "link": b["link"],
                        "kind": "debar"})
        for a in awards:
            due = a["due_date"]
            if not due or not a["time"]:
                continue
            start = _dt.date.fromisoformat(a["time"][:10])
            end = _dt.date.fromisoformat(due)
            total = (end - start).days
            if total <= 0:
                continue
            share = (t - start).days / total
            done = [r for r in rs if r["kind"] == "milestone" and (r["time"] or "") >= a["time"]]
            if share >= LAG_SHARE and not done:
                out.append({"flag": "EXECUTION LAG WATCH", "symbol": sym, "time": a["time"], "title": a["title"],
                            "link": a["link"], "kind": "award", "value_cr": a["value_cr"], "due_date": due,
                            "elapsed_pct": round(min(share, 9.99) * 100, 1),
                            "why": "%.0f%% of the timeline to %s has passed with no milestone filing since the award"
                                   % (min(share, 9.99) * 100, due)})
    return out


# ---------------------------------------------------------------- items

def make_item(src, title, detail, time_, link, symbol="", company="", outlet="", desc="", how="", client_=None,
              mcap=None, usd_inr=USD_INR):
    text = "%s %s" % (title, detail)
    kind, etype = classify(text, desc)
    if not kind:
        return None
    value = order_value_cr(text, usd_inr) if kind in ("award", "cancel", "delay") else None
    cr = mcap(symbol) if (mcap and symbol and value) else None
    impact = round(value / cr * 100, 2) if (value and cr) else None
    months = timeline_months(text) if kind == "award" else None
    own = text
    for name in (company, symbol):            # the client is never the company itself (Rail Vikas Nigam)
        if name and len(name) > 2:
            own = re.sub(r"\b%s\b" % re.escape(name), " ", own, flags=re.I)
    if company:
        own = re.sub(re.escape(im.norm_name(company)), " ", own, flags=re.I)
    lab, sec = client_of(own)
    nums = {k: v for k, v in (("value_cr", value), ("mcap_cr", cr), ("impact_pct", impact), ("timeline_months", months),
                              ("due_date", due_date(text, time_) if kind == "award" else None)) if v is not None}
    nums.update(results_change(text) if kind == "results" else {})
    flags = [LABEL[kind]]
    if impact is not None and impact >= HIGH_IMPACT:
        flags.append("HIGH IMPACT")
    if lab:
        flags.append(lab)
    if how == "symbol":
        flags.append("matched on the symbol")
    return {"id": "%s|%s" % (src, link or title), "source": src, "tag": "orders", "tags": ["orders"], "kind": kind,
            "event_type": etype, "direction": DIRECTION[kind], "symbol": symbol, "company": company,
            "title": title[:200], "detail": re.sub(r"\s+", " ", detail or "")[:400], "time": time_, "link": link,
            "numbers": nums, "flags": flags, "client": lab, "sector": sec, "outlet": outlet}


def gnews_url(query, days=1):
    return GNEWS % (re.sub(r"\s+", "+", query.strip().replace('"', "%22")), max(1, days))


def strip_outlet(title):
    """Google News titles end in ' - Outlet'."""
    m = re.match(r"^(.*\S)\s+-\s+([^-]{2,60})$", title or "")
    return (m.group(1), m.group(2)) if m else (title, "")


class Collector(object):
    def __init__(self, client, args, notes):
        self.client, self.args, self.notes = client, args, notes
        self._matcher = None
        self.feeds = ih.load_json(args.feeds) or {}
        self._mcaps = {}

    def matcher(self):
        if self._matcher is None:
            try:
                master = im.equity_master(self.client, self.args.cache)
            except ih.FeedError as e:
                self.notes.append("company names: NSE's EQUITY_L.csv did not answer (%s); news cannot be matched"
                                  % str(e)[:100])
                master = {}
            self._matcher = im.NameMatcher(master, ih.load_json(ALIASES) or {})
        return self._matcher

    def mcap(self, sym):
        if sym not in self._mcaps:
            self._mcaps[sym] = im.market_cap_cr(self.client, sym, self.args.cache, notes=self.notes)
        return self._mcaps[sym]

    def item(self, src, title, detail, when, link, **kw):
        return make_item(src, title, detail, when, link, mcap=self.mcap, usd_inr=self.args.usd_inr, **kw)

    # -- exchange filings: they carry the symbol
    def nse(self):
        to = ih.now_ist().date()
        frm = to - _dt.timedelta(days=self.args.days)
        rows = self.client.nse_api("/api/corporate-announcements?index=equities&from_date=%s&to_date=%s"
                                   % (frm.strftime("%d-%m-%Y"), to.strftime("%d-%m-%Y")), "nse_announcements.json") or []
        out = []
        for a in rows:
            desc, text = a.get("desc") or "", a.get("attchmntText") or ""
            it = self.item("NSE announcements", "%s: %s" % (a.get("symbol") or a.get("sm_name"), desc), text,
                           ih.parse_when(a.get("an_dt") or a.get("sort_date")), a.get("attchmntFile") or "",
                           symbol=(a.get("symbol") or "").upper(), company=a.get("sm_name") or "", desc=desc)
            if it and it["kind"] != "results":            # exchange results are trader-news' job
                out.append(it)
        return out

    def bse(self):
        import news   # the BSE collector and its RSS fallback, shared with news.py
        out = []
        for b in news.collect_bse(self.client, self.args.days, self.notes):
            hits = self.matcher().find(b["company"]) if b["company"] else []
            sym = hits[0][0] if hits else ""
            it = self.item("BSE announcements", b["title"], b["detail"], b["time"], b["link"], symbol=sym,
                           company=b["company"], desc=(b["flags"] or [""])[0])
            if it and it["kind"] != "results":
                out.append(it)
        return out

    # -- news and government feeds: the company must be named
    def _news(self, src, entries, outlet=""):
        out = []
        for e in entries:
            title, out_name = strip_outlet(e["title"])
            summary = re.sub(r"<[^>]+>", " ", e.get("summary") or "")
            hits = self.matcher().find(title)
            if not hits:
                continue
            for sym, how in hits[:3]:
                it = self.item(src, title, summary, e["time"], e["link"], symbol=sym,
                               company=self.matcher_name(sym), outlet=e.get("outlet") or out_name or outlet, how=how)
                if it:
                    it["id"] = "%s|%s|%s" % (src, sym, e["link"] or title)
                    out.append(it)
        return out

    def matcher_name(self, sym):
        for n, s in self.matcher().names.items():
            if s == sym:
                return n.title()
        return ""

    def _rss(self, urls, src, label):
        out, dead = [], set()
        for url in urls:
            host = url.split("/")[2]
            if host in dead:
                continue
            try:
                out += self._news(src, ih.parse_rss(self.client.get_bytes(url)), outlet=label)
            except ih.FeedError as e:
                if "could not reach" in str(e) or "refuses this network" in str(e):
                    dead.add(host)       # one note per unreachable site, and no more tries this pass
                    self.notes.append("%s: %s is unreachable from here; its feeds were skipped this pass (%s)"
                                      % (src, host, str(e)[:90]))
                else:
                    self.notes.append("%s: %s" % (src, str(e)[:160]))
        return out

    def gnews(self):
        qs = self.feeds.get("gnews_queries") or GNEWS_QUERIES
        urls = [gnews_url(q, self.args.days) for q in qs]
        return self._rss(urls, "Google News", "")

    def press(self):
        return self._rss(self.feeds.get("press") or PRESS, "press RSS", "")

    def pib(self):
        return self._rss(self.feeds.get("pib") or PIB, "PIB", "Press Information Bureau")

    # -- procurement portals: best effort, never past a CAPTCHA
    def _portal(self, urls, src):
        out = []
        for url in urls:
            try:
                body = self.client.get_text(url)
            except ih.FeedError as e:
                self.notes.append("%s: %s" % (src, str(e)[:160]))
                continue
            if CAPTCHA.search(body):
                self.notes.append("%s: %s asks for a CAPTCHA or a login; skipped (never solved)" % (src, url))
                continue
            text = re.sub(r"<[^>]+>", " ", body)
            text = re.sub(r"\s+", " ", text)
            for chunk in re.split(r"(?=GEM/20\d\d/[A-Z]/\d+)|(?<=\.)\s(?=[A-Z])", text):
                if len(chunk) < 30:
                    continue
                hits = self.matcher().find(chunk)
                for sym, how in hits[:2]:
                    word = ("debarred " if src == "CPPP" else "")
                    it = self.item(src, "%s: %s%s" % (sym, word, chunk[:160]), chunk[:400],
                                   ih.now_ist().strftime("%Y-%m-%d %H:%M"), url, symbol=sym, how=how)
                    if it:
                        it["id"] = "%s|%s|%s" % (src, sym, chunk[:80])
                        out.append(it)
        if not out:
            self.notes.append("%s: nothing matched a listed company (the portal's layout may have changed)" % src)
        return out

    def gem(self):
        return self._portal(self.feeds.get("gem") or GEM, "GeM")

    def cppp(self):
        return self._portal(self.feeds.get("cppp") or CPPP, "CPPP")


def collect(client, args, notes, due=None):
    """One pass over the sources that are due. Returns new-or-not items, newest first."""
    only = [s for s in (args.only.split(",") if args.only else SOURCES)]
    bad = set(only) - set(SOURCES)
    if bad:
        raise SystemExit("govt_orders_tracker.py: unknown source(s) %s; choose from %s"
                         % (", ".join(sorted(bad)), ", ".join(SOURCES)))
    col = Collector(client, args, notes)
    items = []
    for name in only:
        if due is not None and name not in due:
            continue
        try:
            items += getattr(col, name)()
        except ih.FeedError as e:
            notes.append("%s: %s" % (name, str(e)[:200]))
        except Exception as e:  # one broken source never stops the others
            notes.append("%s: unexpected %s: %s" % (name, type(e).__name__, str(e)[:200]))
    uniq, seen = {}, set()
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq[it["id"]] = it
    since = (ih.now_ist() - _dt.timedelta(days=args.days + 1)).strftime("%Y-%m-%d")
    now = ih.now_ist().strftime("%Y-%m-%d %H:%M")      # with --now, nothing published after it
    out = [i for i in uniq.values() if not i["time"] or since <= i["time"] <= now]
    return sorted(out, key=lambda i: i["time"] or "", reverse=True)


def attach_flags(items, flags):
    """Put the ledger's flags on the run's items, and add a flag item for each one not in the run."""
    by = {}
    for f in flags:
        by.setdefault(f["symbol"], []).append(f)
    for it in items:
        for f in by.get(it["symbol"], []):
            if f["flag"] not in it["flags"]:
                it["flags"].append(f["flag"])
    out = list(items)
    have = {i["link"] for i in items}
    for f in flags:
        if f["link"] in have:
            continue
        out.append({"id": "ledger|%s|%s|%s" % (f["flag"], f["symbol"], f["link"]), "source": "orders ledger",
                    "tag": "orders", "tags": ["orders"], "kind": f["kind"], "direction": "down", "symbol": f["symbol"],
                    "company": "", "title": "%s %s: %s" % (f["symbol"], f["flag"], f["title"][:150]),
                    "detail": f.get("why") or ("order: %s (matched by %s)" % (f.get("order") or "-", f.get("matched_by"))),
                    "time": f["time"], "link": f["link"],
                    "numbers": {k: f[k] for k in ("value_cr", "due_date", "elapsed_pct") if f.get(k) is not None},
                    "flags": [f["flag"]], "client": "", "sector": "", "outlet": ""})
    return out


# ---------------------------------------------------------------- output

def money(x):
    return "Rs %s cr" % ("{:,.0f}".format(x) if x >= 100 else "{:,.2f}".format(x))


def line(it):
    n = it["numbers"]
    bits = [it["flags"][0] if it["flags"] else it["kind"]]
    if n.get("value_cr"):
        bits.append(money(n["value_cr"]))
    if n.get("impact_pct") is not None:
        bits.append("%.1f%% of market cap" % n["impact_pct"])
    if n.get("timeline_months"):
        bits.append("%g months" % n["timeline_months"])
    if n.get("profit_yoy") is not None:
        bits.append("profit %+g%%" % n["profit_yoy"])
    extra = [f for f in it["flags"][1:] if f]
    mark = "*" if ("HIGH IMPACT" in it["flags"] or it["direction"] == "down") else " "
    return "%s %s  %-11s %s\n      %s%s\n      %s%s" % (
        mark, (it["time"] or "")[:16], it["symbol"] or "?", it["title"][:110], ", ".join(bits),
        ("  [" + ", ".join(extra) + "]") if extra else "", it["link"],
        ("  (%s)" % it["outlet"]) if it.get("outlet") else "")


def render(shown, flags, notes, checked):
    out = ["Government orders & execution risk, %s IST: %d item(s)" % (checked, len(shown))]
    if not shown:
        out.append("  nothing new")
    for it in shown:
        out.append(line(it))
    if flags:
        out.append("Ledger flags (%d):" % len(flags))
        for f in flags:
            out.append("  ! %-26s %-11s %s  %s" % (f["flag"], f["symbol"], (f.get("why") or f["title"])[:90], f["link"]))
    for n in notes:
        out.append("  note: %s" % n)
    out.append("Leads, not signals: open the filing before acting. Nothing here places an order.")
    return "\n".join(out) + "\n"


def read_pdfs(client, items, args, con, notes):
    """An exchange filing's text is one line ("has informed the Exchange about Bagging/Receiving of
    orders"); the value, timeline and client are in the PDF. The first --pdf-pages pages of up to
    --pdf-max new order PDFs a pass are read (pypdf / pdfplumber, as in news.py); a PDF already in the
    ledger is never fetched again."""
    if args.no_pdf:
        return
    import news
    done = 0
    for it in items:
        if done >= args.pdf_max:
            notes.append("PDFs: read %d this pass; the rest wait for the next" % done)
            break
        if it["kind"] not in ("award", "cancel", "delay") or it["numbers"].get("value_cr") \
                or not (it["link"] or "").lower().endswith(".pdf"):
            continue
        if con is not None and con.execute("SELECT 1 FROM items WHERE id=?", (it["id"],)).fetchone():
            continue
        done += 1
        try:
            text = news.pdf_text(client.get_bytes(it["link"], max_bytes=15 * 1048576), pages=args.pdf_pages)
        except Exception as e:  # a broken or huge PDF never stops the pass
            notes.append("PDF %s: %s" % (it["link"].rsplit("/", 1)[-1][:60], str(e)[:100]))
            continue
        if not text.strip():
            it["flags"].append("PDF is a scan: no text to read")
            continue
        again = make_item(it["source"], it["title"], text, it["time"], it["link"], symbol=it["symbol"],
                          company=it["company"], desc="", mcap=lambda s: im.market_cap_cr(client, s, args.cache,
                                                                                          notes=notes),
                          usd_inr=args.usd_inr)
        if again:
            for k, v in again["numbers"].items():
                it["numbers"].setdefault(k, v)
            for f in again["flags"][1:]:
                if f not in it["flags"]:
                    it["flags"].append(f)
            if not it["client"] and again["client"]:
                it["client"], it["sector"] = again["client"], again["sector"]
            it["detail"] = re.sub(r"\s+", " ", text)[:400]


def run_once(client, args, state, con, due=None):
    notes = []
    items = collect(client, args, notes, due)
    read_pdfs(client, items, args, con, notes)
    ih.fresh(items, state)
    checked = ih.now_ist().strftime("%Y-%m-%d %H:%M")
    if con is not None:
        store(con, items, checked)
        flags = ledger_flags(con, ih.now_ist().strftime("%Y-%m-%d"))
    else:
        flags = []
    items = attach_flags(items, flags)
    for it in items:
        it.setdefault("new", state.is_new(it["id"]))
    shown = items if args.all else [i for i in items if i["new"]]
    doc = {"checked": checked, "items": shown, "watch": flags, "notes": notes}
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
    else:
        sys.stdout.write(render(shown, flags, notes, checked))
    if not args.dry_run:
        ih.write_json(os.path.join(args.out, "latest.json"), doc)
        ih.append_csv(os.path.join(args.out, "alerts.csv"), [i for i in items if i["new"]], CSV_FIELDS)
        for i in items:
            if i["new"]:
                state.mark(i["id"])
        state.save()
    return shown


def cmd_ledger(args):
    con = connect(args.db)
    flags = ledger_flags(con, ih.now_ist().strftime("%Y-%m-%d"))
    rows = _rows(con, "WHERE kind='award' AND symbol != ''")
    con.close()
    print("Order ledger %s: %d awards on file" % (args.db, len(rows)))
    for r in rows[-40:]:
        print("  %s %-11s %-12s %-22s %s" % ((r["time"] or "")[:10], r["symbol"],
                                             money(r["value_cr"]) if r["value_cr"] else "value ?",
                                             ("due %s" % r["due_date"]) if r["due_date"] else "no timeline",
                                             (r["title"] or "")[:70]))
    for f in flags:
        print("  ! %-26s %-11s %s" % (f["flag"], f["symbol"], (f.get("why") or f["title"])[:100]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Government order, tender & execution risk tracker (trader-news).")
    ap.add_argument("command", nargs="?", default="run", choices=("run", "ledger"))
    ap.add_argument("--only", help="comma-separated sources: " + ",".join(SOURCES))
    ap.add_argument("--days", type=int, default=1, help="look-back window in days (1)")
    ap.add_argument("--all", action="store_true", help="show items already flagged too")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="keep polling: every SECONDS in market hours")
    ap.add_argument("--idle", type=int, default=900, help="seconds between passes outside market hours (900)")
    ap.add_argument("--max-passes", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--pdf-pages", type=int, default=3, help="PDF pages to read for the value (3)")
    ap.add_argument("--pdf-max", type=int, default=10, help="order PDFs to read per pass (10)")
    ap.add_argument("--no-pdf", action="store_true", help="do not download PDFs")
    ap.add_argument("--usd-inr", type=float, default=USD_INR, help="rupees per US dollar for USD orders (85)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--feeds", default=FEEDS_FILE)
    ap.add_argument("--cache", default=im.CACHE)
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse, write and remember nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-23 13:05"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers from DIR, no network")
    ap.add_argument("--record", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    ih.pin_clock(args.now)
    if args.command == "ledger":
        return cmd_ledger(args)
    client = ih.Client(offline=args.offline, record=args.record)
    state = ih.State(args.state)
    con = None if args.dry_run else connect(args.db)
    if args.dry_run:
        con = sqlite3.connect(":memory:")
        con.executescript(SCHEMA)
    try:
        if not args.watch:
            shown = run_once(client, args, state, con)
            return 0 if shown else 1
        last, passes = {}, 0
        while True:
            now = time.time()
            due = {s for s in SOURCES if now - last.get(s, 0) >= MIN_INTERVAL[s]}
            run_once(client, args, state, con, due)
            for s in due:
                last[s] = now
            passes += 1
            if args.max_passes and passes >= args.max_passes:
                return 0
            time.sleep(args.watch if im.market_hours() else max(args.watch, args.idle))
    except SystemExit as e:
        if e.code in (None, 0):
            raise
        print(e.code if isinstance(e.code, str) else "govt_orders_tracker.py: stopped", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
