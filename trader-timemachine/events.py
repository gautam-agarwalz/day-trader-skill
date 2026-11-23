#!/usr/bin/env python3
"""Event study for NSE stocks: what happened after past filings of each kind, and trade ideas from
today's filings. Overnight gap (buy near the close, sell at the next open) and swing holds (1 to 10
days), from five years of NSE announcements and NSE's daily bhavcopy. No language model, no API key.

    python3 events.py backfill --years 5                 # filings, daily bars, bands, deals -> sqlite (resumable)
    python3 events.py update                             # just the days since the last backfill
    python3 events.py types                              # how many past events of each type
    python3 events.py study --type order_win             # what followed, by day-0 move, and the best rule
    python3 events.py study --type results --session during --bucket "up 2-5%"
    python3 events.py suggest                            # ideas from today's trader-news / trader-pelosi items
    python3 events.py plan 1 --write                     # idea 1 -> trader/plan.json (swing) for budget and risk

Data (all official NSE, public):
  www.nseindia.in/api/corporate-announcements?index=equities&from_date=&to_date=   every filing since 2016
  nsearchives.nseindia.com/products/content/sec_bhavdata_full_<DDMMYYYY>.csv     daily bars, delivery %
  nsearchives.nseindia.com/content/equities/eq_band_changes_<DDMMYYYY>.csv       price-band changes
  www.nseindia.in/api/historicalOR/bulk-block-short-deals                         bulk and block deals
Stored in trader/history/events.sqlite. Ideas go to trader/ideas/latest.json and ideas.csv.

How a past event is measured. Each filing's NSE time decides its session: before 09:15 (pre), during
the session (during), or after 15:30 / on a holiday (after); day 0 is the first session the price
could react in. Prices are chained from NSE's previous close, which NSE adjusts on ex-dates, so
splits and bonuses do not show up as moves. Costs (default 0.30 % a round trip, delivery) come off
every trade. A day-0 close locked at the upper circuit counts as not buyable.

How the odds are made. For every rule in a grid (entry at the day-0 close / the next open / the
day-0 open; stop 1 to 3 x the stock's ATR; target 1 to 3 x the risk; exit by day 1 to 10, plus the
plain overnight gap and plain holds) each past event is replayed on its real bars. The rule with the
best lower bound on the average is chosen on events up to 12 months ago, then 10,000 bootstrap
resamples (fixed seed) give the odds and the range, and the last 12 months check it out of sample.
History, not a forecast: rare types fall back to their family; fewer than 30 events, no idea.
"""

import argparse
import bisect
import csv
import datetime as _dt
import io
import json
import math
import os
import re
import sqlite3
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import india_http as ih  # noqa: E402
import india_catalysts as ic  # noqa: E402

DB_PATH = os.path.join("trader", "history", "events.sqlite")
IDEAS_DIR = os.path.join("trader", "ideas")
BASE = "trader"
ANN = "/api/corporate-announcements?index=equities&from_date=%s&to_date=%s"
MEETINGS = "/api/corporate-board-meetings?index=equities&from_date=%s&to_date=%s"
DEALS = "/api/historicalOR/bulk-block-short-deals?optionType=%s&from=%s&to=%s"
BHAV = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_%s.csv"
BANDS = "https://nsearchives.nseindia.com/content/equities/eq_band_changes_%s.csv"
FO_LOTS = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
FO_BAN = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
SOURCES = ("bars", "filings", "meetings", "bands", "deals", "fno")
FIRST_BHAV = _dt.date(2020, 12, 1)     # the full bhavcopy (with delivery) starts here
SERIES = ("EQ", "BE")
OPEN_HM, CLOSE_HM = "09:15", "15:30"
COST = 0.30            # % a round trip for a delivery trade (STT, charges, slippage)
MIN_EVENTS = 30
MIN_TURNOVER_CR = 1.0  # median daily turnover over the 20 sessions before day 0
SIMS = 10000
SEED = 7
STOPS = (1.0, 1.5, 2.0, 3.0)      # x ATR
TARGETS = (1.0, 1.5, 2.0, 3.0)    # x risk
HORIZONS = (1, 2, 3, 5, 10)
MAX_EVENTS = 6000                 # most recent events used per study (speed)
OOS_MONTHS = 12
BANDS_PCT = (0.02, 0.05, 0.10, 0.20)
NSE_HOLIDAYS = {  # NSE equity segment trading holidays on weekdays, 2026 (NSE circulars)
    "2026-01-15": "Municipal Corporation Elections in Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
BUCKETS = ((-1e9, -5, "down >5%"), (-5, -2, "down 2-5%"), (-2, 0, "down 0-2%"),
           (0, 2, "up 0-2%"), (2, 5, "up 2-5%"), (5, 1e9, "up >5%"))
ENTRY_TEXT = {"close0": "buy at the day-0 close (15:10-15:25)", "open1": "buy at the open the session after day 0",
              "open0": "buy at the day-0 open (the filing came before the session)"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, symbol TEXT, time TEXT, type TEXT, direction TEXT,
  category TEXT, text TEXT, link TEXT, source TEXT, event_date TEXT, value_cr REAL);
CREATE INDEX IF NOT EXISTS ev_type ON events(type, time);
CREATE INDEX IF NOT EXISTS ev_sym ON events(symbol, time);
CREATE TABLE IF NOT EXISTS bars(symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
  prev_close REAL, volume REAL, turnover_cr REAL, deliv REAL, PRIMARY KEY(symbol, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, market REAL, n INTEGER);
CREATE TABLE IF NOT EXISTS loaded(kind TEXT, key TEXT, at TEXT, rows INTEGER, PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS fno(symbol TEXT PRIMARY KEY, lot INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS fno_ban(date TEXT, symbol TEXT, PRIMARY KEY(date, symbol));
"""


# ---------------------------------------------------------------- the database

def connect(path=DB_PATH):
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def is_loaded(con, kind, key):
    return con.execute("SELECT 1 FROM loaded WHERE kind=? AND key=?", (kind, key)).fetchone() is not None


def mark_loaded(con, kind, key, rows):
    con.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?)",
                (kind, key, ih.now_ist().strftime("%Y-%m-%d %H:%M"), rows))


def add_events(con, events):
    con.executemany("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(e["id"], e["symbol"], e["time"], e["type"], e["direction"], e.get("category", ""),
                      (e.get("text") or "")[:400], e.get("link", ""), e.get("source", ""), e.get("event_date"),
                      e.get("value_cr")) for e in events])


def add_bars(con, day, rows):
    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(r["symbol"], day, r["open"], r["high"], r["low"], r["close"], r["prev_close"], r["volume"],
                      r["turnover_cr"], r["deliv"]) for r in rows])
    rets = [r["close"] / r["prev_close"] - 1 for r in rows
            if r["prev_close"] and r["close"] and (r["turnover_cr"] or 0) >= MIN_TURNOVER_CR and r["series"] == "EQ"]
    con.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)",
                (day, statistics.median(rets) if rets else 0.0, len(rows)))


# ---------------------------------------------------------------- parsers

def _f(x):
    x = (x or "").strip()
    if x in ("", "-"):
        return None
    try:
        return float(x.replace(",", ""))
    except ValueError:
        return None


def parse_bhavcopy(text, series=SERIES):
    """sec_bhavdata_full CSV -> (YYYY-MM-DD, [row]). Only series EQ and BE (the tradeable equities)."""
    rows, day = [], None
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().upper() for h in next(reader, [])]
    if "SYMBOL" not in head:
        return None, []
    ix = {h: i for i, h in enumerate(head)}
    for rec in reader:
        if len(rec) < len(head):
            continue
        g = lambda k: rec[ix[k]].strip() if k in ix else ""  # noqa: E731
        if g("SERIES") not in series:
            continue
        if day is None:
            try:
                day = _dt.datetime.strptime(g("DATE1"), "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
        o, h, lo, c, pc = (_f(g(k)) for k in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "PREV_CLOSE"))
        if not (o and h and lo and c and pc):
            continue
        t = _f(g("TURNOVER_LACS"))
        rows.append({"symbol": g("SYMBOL"), "series": g("SERIES"), "open": o, "high": h, "low": lo, "close": c,
                     "prev_close": pc, "volume": _f(g("TTL_TRD_QNTY")), "turnover_cr": t / 100.0 if t else None,
                     "deliv": _f(g("DELIV_PER"))})
    return day, rows


def parse_announcements(doc):
    """NSE corporate-announcements JSON -> typed events (routine filings dropped)."""
    out = []
    for a in doc if isinstance(doc, list) else (doc or {}).get("data") or []:
        sym = (a.get("symbol") or "").strip().upper()
        when = (a.get("sort_date") or "").strip()
        if not sym or len(when) < 16:
            continue
        text, desc = a.get("attchmntText") or "", (a.get("desc") or "").strip()
        etype, direction = ic.event_type(text, desc, a.get("sm_name") or "")
        if not etype:
            continue
        ev = {"id": "ann|%s" % (a.get("seq_id") or a.get("attchmntFile") or "%s|%s|%s" % (sym, when, desc)),
              "symbol": sym, "time": when[:16], "type": etype, "direction": direction, "category": desc,
              "text": text, "link": a.get("attchmntFile") or "", "source": "NSE announcements",
              "event_date": ic.meeting_date(text) if etype.startswith("board_meeting") else None}
        out.append(ev)
    return out


def _when(text):
    try:
        return _dt.datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None


def parse_meetings(doc):
    """NSE board-meeting intimations -> board_meeting_* events: the notice time and the meeting date.
    The early signal: a board meeting 'to consider' a buyback, a bonus, a fundraise or results."""
    out = []
    for m in doc if isinstance(doc, list) else (doc or {}).get("data") or []:
        sym = (m.get("bm_symbol") or "").strip().upper()
        when = _when(m.get("bm_timestamp") or "")
        purpose, desc = (m.get("bm_purpose") or "").strip(), (m.get("bm_desc") or "").strip()
        if not sym or not when:
            continue
        etype, direction = ic.event_type("Board meeting to consider %s. %s" % (purpose, desc))
        if not etype or not etype.startswith("board_meeting"):
            continue
        try:
            meet = _dt.datetime.strptime((m.get("bm_date") or "").strip(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            meet = None
        out.append({"id": "bm|%s|%s|%s" % (sym, meet, purpose), "symbol": sym, "time": when, "type": etype,
                    "direction": direction, "category": "Board meeting: " + purpose, "text": desc,
                    "link": m.get("attachment") or "", "source": "NSE board meetings", "event_date": meet})
    return out


def parse_bands(text, day):
    """eq_band_changes CSV for `day` -> band_cut / band_widen events, effective from the next session."""
    out = []
    reader = csv.reader(io.StringIO((text or "").replace("\r\n", "\n").replace("\r", "\n")))
    head = [h.strip().lower() for h in next(reader, [])]
    if "symbol" not in head or "from" not in head:
        return out
    ix = {h: i for i, h in enumerate(head)}
    for rec in reader:
        if len(rec) < len(head) or rec[ix["series"]].strip() not in SERIES:
            continue
        a, b = _f(rec[ix["from"]]), _f(rec[ix["to"]])
        if a is None or b is None or a == b:
            continue
        sym = rec[ix["symbol"]].strip().upper()
        t = "band_cut" if b < a else "band_widen"
        out.append({"id": "band|%s|%s" % (day, sym), "symbol": sym, "time": day + " 18:00", "type": t,
                    "direction": ic.EVENT_DIRECTION[t], "category": "Price band change",
                    "text": "price band %g%% -> %g%%" % (a, b), "link": BANDS % _ddmmyyyy(day),
                    "source": "NSE price band changes"})
    return out


def parse_deals(block_doc, bulk_doc, day):
    """One day's NSE block and bulk deals -> events. Deals are reported after the close.
    Block deals: one block_deal event per stock. Bulk deals: clients who bought and sold the same day
    are dropped as round trips; the rest are netted per stock (>= Rs 1 cr -> bulk_net_buy / sell)."""
    out = []

    def rows(doc):
        return [r for r in ((doc or {}).get("data") or []) if r.get("BD_SYMBOL")]
    blocks = {}
    for r in rows(block_doc):
        v = (r.get("BD_QTY_TRD") or 0) * (r.get("BD_TP_WATP") or 0) / 1e7
        blocks.setdefault(r["BD_SYMBOL"].strip().upper(), []).append((r.get("BD_CLIENT_NAME") or "", r.get("BD_BUY_SELL"), v))
    for sym, ds in blocks.items():
        value = sum(v for _, s, v in ds if s == "BUY") or sum(v for _, _, v in ds)
        out.append({"id": "block|%s|%s" % (day, sym), "symbol": sym, "time": day + " 18:00", "type": "block_deal",
                    "direction": "?", "category": "Block deal", "value_cr": round(value, 2),
                    "text": "; ".join("%s %s Rs %.1f cr" % (c, s, v) for c, s, v in ds)[:400],
                    "source": "NSE block deals", "link": "https://www.nseindia.in/report-detail/display-bulk-and-block-deals"})
    per = {}
    for r in rows(bulk_doc):
        sym, client = r["BD_SYMBOL"].strip().upper(), (r.get("BD_CLIENT_NAME") or "").strip().upper()
        q = (r.get("BD_QTY_TRD") or 0) * (1 if r.get("BD_BUY_SELL") == "BUY" else -1)
        d = per.setdefault(sym, {}).setdefault(client, {"buy": 0, "sell": 0, "value": 0.0})
        d["buy" if q > 0 else "sell"] += 1
        d["value"] += q * (r.get("BD_TP_WATP") or 0) / 1e7
    for sym, clients in per.items():
        net = sum(c["value"] for c in clients.values() if not (c["buy"] and c["sell"]))
        if abs(net) < 1.0:
            continue
        t = "bulk_net_buy" if net > 0 else "bulk_net_sell"
        out.append({"id": "bulk|%s|%s" % (day, sym), "symbol": sym, "time": day + " 18:00", "type": t,
                    "direction": ic.EVENT_DIRECTION[t], "category": "Bulk deal", "value_cr": round(net, 2),
                    "text": "net bulk deals Rs %.1f cr (round trips dropped)" % net, "source": "NSE bulk deals",
                    "link": "https://www.nseindia.in/report-detail/display-bulk-and-block-deals"})
    return out


def parse_lots(text):
    """NSE fo_mktlots.csv -> {SYMBOL: lot size of the nearest expiry}. Index rows are kept (harmless)."""
    out = {}
    reader = csv.reader(io.StringIO((text or "").replace("\r\n", "\n").replace("\r", "\n")))
    head = [h.strip().upper() for h in next(reader, [])]
    if "SYMBOL" not in head:
        return out
    si = head.index("SYMBOL")
    for rec in reader:
        if len(rec) <= si + 1:
            continue
        sym = rec[si].strip().upper()
        lots = [int(float(x)) for x in (c.strip() for c in rec[si + 1:]) if re.match(r"^\d+(\.0+)?$", x)]
        if sym and sym != "SYMBOL" and lots:
            out[sym] = lots[0]
    return out


def parse_ban(text):
    """NSE fo_secban.csv -> (YYYY-MM-DD trade date, [SYMBOL]) of stocks in the F&O ban period."""
    day = None
    m = re.search(r"Trade Date\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", text or "")
    if m:
        day = _dt.datetime.strptime(m.group(1).title(), "%d-%b-%Y").date().isoformat()
    syms = [ln.split(",", 1)[1].strip().upper() for ln in (text or "").splitlines()
            if re.match(r"^\s*\d+\s*,", ln) and len(ln.split(",", 1)) == 2]
    return day, [x for x in syms if x]


def load_fno(con, client, today):
    """Refresh the F&O lot sizes and today's ban list (for the futures-short ideas)."""
    lots = parse_lots(client.get_text(FO_LOTS, "fo_mktlots.csv"))
    if lots:
        con.execute("DELETE FROM fno")
        con.executemany("INSERT INTO fno VALUES (?,?,?)", [(k, v, today.isoformat()) for k, v in lots.items()])
    day, banned = parse_ban(client.get_text(FO_BAN, "fo_secban.csv"))
    if day:
        con.executemany("INSERT OR REPLACE INTO fno_ban VALUES (?,?)", [(day, b) for b in banned])
    con.commit()
    return len(lots), day, banned


def _ddmmyyyy(day):
    return day[8:10] + day[5:7] + day[0:4]


def _dmy(day):
    return day[8:10] + "-" + day[5:7] + "-" + day[0:4]


# ---------------------------------------------------------------- backfill

def _weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += _dt.timedelta(days=1)


def backfill(con, client, start, end, sources=SOURCES, log=print, today=None):
    """Fill the database from `start` to `end` (dates). Resumable: done days and weeks are skipped.
    A missing bhavcopy on a weekday more than 3 days old is a holiday and is remembered as one."""
    today = today or ih.now_ist().date()
    settled = today - _dt.timedelta(days=3)
    stats = {"days": 0, "bars": 0, "events": 0, "holidays": 0, "errors": 0, "calls": 0}
    start = max(start, FIRST_BHAV) if "bars" in sources else start

    def note(msg):
        stats["errors"] += 1
        log("  ! " + msg)
    n = 0
    for d in _weekdays(start, end):
        day = d.isoformat()
        if "bars" in sources and not is_loaded(con, "bars", day):
            try:
                stats["calls"] += 1
                bday, rows = parse_bhavcopy(client.get_text(BHAV % _ddmmyyyy(day), "bhav_%s.csv" % _ddmmyyyy(day)))
                if rows and bday == day:
                    add_bars(con, day, rows)
                    mark_loaded(con, "bars", day, len(rows))
                    stats["days"] += 1
                    stats["bars"] += len(rows)
                elif d < settled:   # on a holiday NSE serves the previous session's file under the new name
                    mark_loaded(con, "bars", day, 0)
                    stats["holidays"] += 1
            except csv.Error as e:
                note("bhavcopy %s: unreadable CSV (%s)" % (day, e))
            except ih.FeedError as e:
                if "HTTP 404" in str(e) and d < settled:
                    mark_loaded(con, "bars", day, 0)
                    stats["holidays"] += 1
                elif "HTTP 404" not in str(e):
                    note("bhavcopy %s: %s" % (day, e))
        trading = con.execute("SELECT rows FROM loaded WHERE kind='bars' AND key=?", (day,)).fetchone()
        trading = bool(trading and trading[0])
        if "bands" in sources and trading and not is_loaded(con, "bands", day):
            try:
                stats["calls"] += 1
                evs = parse_bands(client.get_text(BANDS % _ddmmyyyy(day), "bands_%s.csv" % _ddmmyyyy(day)), day)
                add_events(con, evs)
                stats["events"] += len(evs)
                mark_loaded(con, "bands", day, len(evs))
            except csv.Error as e:
                note("bands %s: unreadable CSV (%s)" % (day, e))
            except ih.FeedError as e:
                if "HTTP 404" in str(e) and d < settled:
                    mark_loaded(con, "bands", day, 0)
                elif "HTTP 404" not in str(e):
                    note("bands %s: %s" % (day, e))
        if "deals" in sources and trading and not is_loaded(con, "deals", day):
            try:
                stats["calls"] += 2
                blk = client.nse_api(DEALS % ("block_deals", _dmy(day), _dmy(day)), "deals_block_%s.json" % day)
                blk_rows = len((blk or {}).get("data") or [])
                bulk = client.nse_api(DEALS % ("bulk_deals", _dmy(day), _dmy(day)), "deals_bulk_%s.json" % day)
                evs = parse_deals(blk, bulk, day)
                add_events(con, evs)
                stats["events"] += len(evs)
                if d < settled:
                    mark_loaded(con, "deals", day, blk_rows)
            except ih.FeedError as e:
                note("deals %s: %s" % (day, e))
        n += 1
        if n % 20 == 0 and {"bars", "bands", "deals"} & set(sources):
            con.commit()
            log("  %s: %d trading days, %d events so far" % (day, stats["days"], stats["events"]))
    if "filings" in sources:
        monday = start - _dt.timedelta(days=start.weekday())
        while monday <= end:
            sunday = monday + _dt.timedelta(days=6)
            key = monday.isoformat()
            if not is_loaded(con, "filings", key):
                try:
                    stats["calls"] += 1
                    doc = client.nse_api(ANN % (_dmy(key), _dmy(min(sunday, end).isoformat())), "ann_%s.json" % key)
                    evs = parse_announcements(doc)
                    add_events(con, evs)
                    stats["events"] += len(evs)
                    if sunday < settled and sunday <= end:
                        mark_loaded(con, "filings", key, len(evs))
                    con.commit()
                    log("  filings week of %s: %d typed events" % (key, len(evs)))
                except ih.FeedError as e:
                    note("filings week %s: %s" % (key, e))
            monday += _dt.timedelta(days=7)
    if "meetings" in sources:   # NSE filters board meetings by the meeting date; notices come up to ~3 weeks ahead
        monday = start - _dt.timedelta(days=start.weekday())
        last = end + _dt.timedelta(days=28)
        while monday <= last:
            sunday = monday + _dt.timedelta(days=6)
            key = monday.isoformat()
            if not is_loaded(con, "meetings", key):
                try:
                    stats["calls"] += 1
                    doc = client.nse_api(MEETINGS % (_dmy(key), _dmy(sunday.isoformat())), "meetings_%s.json" % key)
                    evs = [e for e in parse_meetings(doc) if e["time"][:10] <= end.isoformat()]
                    add_events(con, evs)
                    stats["events"] += len(evs)
                    if sunday < settled and sunday <= end:
                        mark_loaded(con, "meetings", key, len(evs))
                    con.commit()
                except ih.FeedError as e:
                    note("board meetings week %s: %s" % (key, e))
            monday += _dt.timedelta(days=7)
    if "fno" in sources:
        try:
            stats["calls"] += 2
            n_lots, ban_day, banned = load_fno(con, client, today)
            log("  F&O: %d lot sizes; ban list for %s: %s" % (n_lots, ban_day, ", ".join(banned) or "none"))
        except (ih.FeedError, csv.Error) as e:
            note("F&O lots / ban list: %s" % e)
    con.commit()
    return stats


def last_loaded(con, kind="bars"):
    r = con.execute("SELECT max(key) FROM loaded WHERE kind=? AND rows>0", (kind,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- calendar and price paths

class Calendar(object):
    def __init__(self, con):
        self.days = [r[0] for r in con.execute("SELECT date FROM days WHERE n>0 ORDER BY date")]
        self.market = dict(con.execute("SELECT date, market FROM days"))
        self.set = set(self.days)

    def next_day(self, day, strict=True):
        i = bisect.bisect_right(self.days, day) if strict else bisect.bisect_left(self.days, day)
        return self.days[i] if i < len(self.days) else None

    def session(self, time_text):
        """'2026-11-23 13:05' -> (session, day0). day0 None when it is beyond the loaded bars."""
        day, hm = time_text[:10], time_text[11:16] or "00:00"
        if day in self.set:
            if hm < OPEN_HM:
                return "pre", day
            if hm < CLOSE_HM:
                return "during", day
        return "after", self.next_day(day)


def bucket_of(pct):
    for lo, hi, name in BUCKETS:
        if lo <= pct < hi:
            return name
    return BUCKETS[-1][2]


def _locked(o, h, lo, c, pc, up=True):
    r = c / pc - 1
    near = any(abs(abs(r) - b) < 0.0015 for b in BANDS_PCT)
    if h == lo:
        return (r > 0) == up
    return near and ((up and r > 0 and c >= h * 0.9995) or (not up and r < 0 and c <= lo * 1.0005))


def event_path(bars, dates, day0, event_date=None, after=10):
    """The numbers one event is judged on, from its stock's bars (dict date -> row tuple).
    Prices are relative to the day-0 close and chained through NSE's (adjusted) previous close."""
    i0 = bisect.bisect_left(dates, day0)
    if i0 >= len(dates) or dates[i0] != day0 or i0 < 15:
        return None   # day 0 must be a bar of this stock, with 15 sessions before it
    o0, h0, l0, c0, pc0, _v, t0, _dl = bars[dates[i0]]
    trs, turn = [], []
    for j in range(max(0, i0 - 14), i0):
        o, h, lo, c, pc, _v, t, _dl = bars[dates[j]]
        trs.append((max(h, pc) - min(lo, pc)) / pc)
    for j in range(max(0, i0 - 20), i0):
        turn.append(bars[dates[j]][6] or 0.0)
    runup = 1.0
    for j in range(max(0, i0 - 5), i0):
        runup *= bars[dates[j]][3] / bars[dates[j]][4]
    path = []  # day k >= 1: (open, high, low, close) relative to the day-0 close
    level = 1.0
    for j in range(i0 + 1, min(len(dates), i0 + 1 + after)):
        o, h, lo, c, pc, _v, _t, _dl = bars[dates[j]]
        path.append((level * o / pc, level * h / pc, level * lo / pc, level * c / pc))
        level *= c / pc
    hold = None
    if event_date:
        k = bisect.bisect_left(dates, event_date) - 1 - i0   # last session before the meeting
        if 1 <= k <= len(path):
            hold = k
    return {"day0": day0, "r0": c0 / pc0 - 1, "gap0": o0 / pc0 - 1,
            "day0_bar": (o0 / pc0, h0 / pc0, l0 / pc0, c0 / pc0),  # relative to the previous close
            "atr": sum(trs) / len(trs) if trs else None, "turnover": statistics.median(turn) if turn else 0.0,
            "runup5": runup - 1, "path": path, "dates": dates[i0 + 1:i0 + 1 + after],
            "locked_up": _locked(o0, h0, l0, c0, pc0, True), "locked_down": _locked(o0, h0, l0, c0, pc0, False),
            "turnover0": t0 or 0.0, "hold_to_meeting": hold}


def load_bars(con, symbols, before=None):
    """Daily bars per symbol; with `before` ('YYYY-MM-DD ...') only the days before that date."""
    out = {}
    cut = (before or "9999-12-31")[:10]
    for sym in symbols:
        rows = con.execute("SELECT date, open, high, low, close, prev_close, volume, turnover_cr, deliv FROM bars "
                           "WHERE symbol=? AND date<? ORDER BY date", (sym, cut)).fetchall()
        out[sym] = ({r[0]: tuple(r[1:]) for r in rows}, [r[0] for r in rows])
    return out


def collect(con, cal, types, session=None, bucket=None, min_turnover=MIN_TURNOVER_CR, limit=MAX_EVENTS,
            before=None):
    """Past events of these types with their price paths, newest first. One event per stock and day 0.
    With `before` ('YYYY-MM-DD HH:MM') only what was known then: filings before it, bars before its date."""
    q = "SELECT id, symbol, time, type, event_date, category, text, link FROM events WHERE type IN (%s) ORDER BY time DESC" % (
        ",".join("?" * len(types)))
    rows = con.execute(q, list(types)).fetchall()
    picked, seen = [], set()
    for r in rows:
        if before and r[2][:16] >= before[:16]:
            continue
        sess, day0 = cal.session(r[2])
        if not day0 or (before and day0 >= before[:10]) or (session and sess != session) or (r[1], day0) in seen:
            continue
        seen.add((r[1], day0))
        picked.append((r, sess, day0))
        if len(picked) >= limit * 2:
            break
    bars = load_bars(con, sorted({p[0][1] for p in picked}), before)
    out = []
    for r, sess, day0 in picked:
        b, dates = bars.get(r[1], ({}, []))
        p = event_path(b, dates, day0, r[4])
        if not p or p["atr"] is None or not p["path"]:
            continue
        if p["turnover"] < min_turnover:
            continue
        p.update({"id": r[0], "symbol": r[1], "time": r[2], "type": r[3], "session": sess,
                  "bucket": bucket_of(p["r0"] * 100), "link": r[7]})
        if bucket and p["bucket"] != bucket:
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- replaying rules

def simulate(days, entry, stop, target, side=1, entry_at_open=False, exit_at_open=False):
    """Replay one trade on bars [(o, h, l, c)]. stop / target are fractions of the entry (None = none).
    A gap through the stop fills at the open (the real loss); stop and target in one bar count as the
    stop. Returns (return as a fraction, how it ended)."""
    s = entry * (1 - side * stop) if stop else None
    t = entry * (1 + side * target) if target else None
    for i, (o, h, lo, c) in enumerate(days):
        if not (entry_at_open and i == 0):
            if exit_at_open:
                return side * (o / entry - 1), "open"
            if s is not None and side * (o - s) <= 0:
                return side * (o / entry - 1), "stop gap"
            if t is not None and side * (o - t) >= 0:
                return side * (o / entry - 1), "target gap"
        hit_s = s is not None and ((lo <= s) if side > 0 else (h >= s))
        hit_t = t is not None and ((h >= t) if side > 0 else (lo <= t))
        if hit_s:
            return -stop, "stop"
        if hit_t:
            return target, "target"
    c = days[-1][3]
    return side * (c / entry - 1), "time"


def entries_for(session):
    """Which entries a trader could really take for an event in this session."""
    return ("close0", "open1") if session == "during" else ("open0", "close0", "open1")


def trade(ev, rule, side=1):
    """One past event under one rule -> (net return %, outcome) or None when the trade was not possible."""
    entry, kind, n, k, r = rule["entry"], rule["kind"], rule["days"], rule.get("stop_atr"), rule.get("target_r")
    path = ev["path"]
    if entry == "close0":
        if (side > 0 and ev["locked_up"]) or (side < 0 and ev["locked_down"]):
            return None
        days, e, at_open = path[:n], 1.0, False
    elif entry == "open1":
        if not path or (path[0][1] == path[0][2] and side * (path[0][0] - 1) > 0):
            return None
        days, e, at_open = path[:n], path[0][0], True
    else:  # open0: rebase the day-0 bar to the previous close
        o0, h0, l0, c0 = ev["day0_bar"]
        if h0 == l0:
            return None
        days = [(o0, h0, l0, c0)] + [(o * c0, h * c0, lo * c0, c * c0) for o, h, lo, c in path[:max(0, n - 1)]]
        e, at_open = o0, True
    if not days:
        return None
    if kind == "gap":
        if entry != "close0" or not path:
            return None
        ret, how = simulate(path[:1], 1.0, None, None, side, exit_at_open=True)
    else:
        if n > len(path) + (1 if entry == "open0" else 0):
            return None
        stop = k * ev["atr"] if kind == "bracket" else None
        target = r * stop if kind == "bracket" else None
        ret, how = simulate(days, e, stop, target, side, entry_at_open=at_open)
    return ret * 100 - rule["cost"], how


def rule_grid(entries, cost=COST, meeting=False):
    rules = []
    for e in entries:
        if e == "close0":
            rules.append({"entry": e, "kind": "gap", "days": 1, "cost": cost})
        for n in HORIZONS:
            rules.append({"entry": e, "kind": "hold", "days": n, "cost": cost})
            for k in STOPS:
                for r in TARGETS:
                    rules.append({"entry": e, "kind": "bracket", "days": n, "stop_atr": k, "target_r": r, "cost": cost})
    return rules


def describe_rule(rule, side=1):
    e = ENTRY_TEXT[rule["entry"]]
    if side < 0:
        e = e.replace("buy", "short (sell first)")
    if rule["kind"] == "gap":
        return e + ", sell at the next open"
    if rule["kind"] == "hold":
        return "%s, sell at the close of day %d" % (e, rule["days"])
    return "%s, stop %g x ATR, target %g x risk, else sell at the close of day %d" % (
        e, rule["stop_atr"], rule["target_r"], rule["days"])


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def bootstrap(rets, sims=SIMS, seed=SEED):
    """10,000 resamples of the past trades: the range of the average and of the win rate."""
    if not rets:
        return None
    try:
        import numpy as np
        rng = np.random.default_rng(seed)
        a = np.asarray(rets, dtype=float)
        means, wins = [], []
        chunk = max(1, min(sims, 2000000 // max(1, len(a))))
        done = 0
        while done < sims:
            m = min(chunk, sims - done)
            idx = rng.integers(0, len(a), size=(m, len(a)))
            s = a[idx]
            means.extend(s.mean(axis=1).tolist())
            wins.extend((s > 0).mean(axis=1).tolist())
            done += m
    except ImportError:  # pandas brings numpy, but keep working without it
        import random
        rnd = random.Random(seed)
        means, wins = [], []
        for _ in range(min(sims, 2000)):
            s = rnd.choices(rets, k=len(rets))
            means.append(sum(s) / len(s))
            wins.append(sum(1 for x in s if x > 0) / len(s))
    return {"sims": len(means), "mean_p5": _pct(means, 0.05), "mean_p50": _pct(means, 0.5), "mean_p95": _pct(means, 0.95),
            "win_p5": _pct(wins, 0.05) * 100, "win_p95": _pct(wins, 0.95) * 100,
            "p_edge": sum(1 for m in means if m > 0) / len(means) * 100}


def score(results):
    """Stats of one rule over past events: results = [(net %, outcome)]."""
    rets = [x for x, _ in results]
    n = len(rets)
    if not n:
        return None
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    hows = {}
    for _, h in results:
        hows[h] = hows.get(h, 0) + 1
    return {"n": n, "mean": mean, "median": statistics.median(rets), "win": sum(1 for x in rets if x > 0) / n * 100,
            "p5": _pct(rets, 0.05), "p25": _pct(rets, 0.25), "p75": _pct(rets, 0.75), "p95": _pct(rets, 0.95),
            "lower": mean - 1.645 * sd / math.sqrt(n), "sd": sd,
            "outcomes": {k: round(v / n * 100, 1) for k, v in sorted(hows.items())},
            "gap_through": round(hows.get("stop gap", 0) / n * 100, 1)}


def best_rule(events, side=1, entries=None, cost=COST, split=None, sims=SIMS, keep=None):
    """Pick the rule on events before `split` (by lower bound on the average), check it after."""
    entries = entries or ("close0", "open1")
    ins = [e for e in events if not split or e["day0"] < split]
    oos = [e for e in events if split and e["day0"] >= split]
    checked = True
    if len(ins) < MIN_EVENTS:   # a short history: choose on everything, no out-of-sample check possible
        ins, oos, checked = events, [], False
    best = None
    for rule in rule_grid(entries, cost):
        if keep and not keep(rule):
            continue
        res = [x for x in (trade(e, rule, side) for e in ins) if x]
        if len(res) < MIN_EVENTS:
            continue
        s = score(res)
        if best is None or s["lower"] > best[1]["lower"]:
            best = (rule, s, res)
    if not best:
        return None
    rule, s, res = best
    s["bootstrap"] = bootstrap([x for x, _ in res], sims)
    out_res = [x for x in (trade(e, rule, side) for e in oos) if x]
    o = score(out_res) if out_res else None
    verdict = "edge" if s["bootstrap"]["mean_p5"] > 0 else "weak" if s["mean"] > 0 else "none"
    if o and o["n"] >= 10 and o["mean"] <= 0:
        verdict = "failed out of sample"
    elif verdict == "edge" and not (o and o["n"] >= 10):
        verdict = "edge, not checked out of sample"
    return {"rule": rule, "text": describe_rule(rule, side), "in_sample": s, "out_of_sample": o, "verdict": verdict,
            "side": "long" if side > 0 else "short", "oos_checked": checked}


def summarise(events, market=None):
    """The plain numbers behind the table: overnight gap, day 1, day 5, day 10, MFE / MAE, run-up."""
    def col(f):
        xs = [x for x in (f(e) for e in events) if x is not None]
        return {"n": len(xs), "mean": sum(xs) / len(xs) * 100 if xs else None,
                "median": statistics.median(xs) * 100 if xs else None,
                "up": sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else None}
    gap = col(lambda e: e["path"][0][0] - 1 if e["path"] and not e["locked_up"] else None)
    d1 = col(lambda e: e["path"][0][3] - 1 if e["path"] else None)
    d5 = col(lambda e: e["path"][4][3] - 1 if len(e["path"]) >= 5 else None)
    d10 = col(lambda e: e["path"][9][3] - 1 if len(e["path"]) >= 10 else None)
    mfe = col(lambda e: max(p[1] for p in e["path"][:10]) - 1 if e["path"] else None)
    mae = col(lambda e: min(p[2] for p in e["path"][:10]) - 1 if e["path"] else None)
    day0 = col(lambda e: e["r0"])
    gap0 = col(lambda e: e["gap0"])
    runup = col(lambda e: e["runup5"])
    meet = col(lambda e: e["path"][e["hold_to_meeting"] - 1][3] - 1 if e.get("hold_to_meeting") else None)
    market = market or {}

    def excess(e, k):
        if len(e["path"]) < k or not market:
            return None
        m = 1.0
        for d in e["dates"][:k]:
            m *= 1 + market.get(d, 0.0)
        return e["path"][k - 1][3] - m
    x1 = col(lambda e: excess(e, 1))
    x5 = col(lambda e: excess(e, 5))
    return {"n": len(events), "day0": day0, "gap0": gap0, "runup5": runup, "overnight": gap, "day1": d1, "day5": d5,
            "day10": d10, "mfe10": mfe, "mae10": mae, "to_meeting": meet, "day1_vs_market": x1, "day5_vs_market": x5,
            "locked_up": sum(1 for e in events if e["locked_up"])}


def split_date(cal, before=None):
    days = [d for d in cal.days if not before or d < before[:10]]
    if not days:
        return None
    last = _dt.date.fromisoformat(days[-1])
    return (last - _dt.timedelta(days=int(OOS_MONTHS * 30.4))).isoformat()


def study(con, etype, session=None, bucket=None, side=1, cost=COST, min_turnover=MIN_TURNOVER_CR, sims=SIMS,
          cal=None, fallback=True, entries=None, keep=None, before=None):
    """Everything about one event type (falling back to its family when there are too few).
    `before` limits it to what was known at that time (no look-ahead when replaying a past day)."""
    cal = cal or Calendar(con)
    used, types = etype, [etype]
    events = collect(con, cal, types, session, bucket, min_turnover, before=before)
    note = None
    if len(events) < MIN_EVENTS and fallback:
        fam = ic.family_of(etype)
        members = [t for t in ic.ALL_TYPES if ic.family_of(t) == fam]
        if len(members) > 1:
            more = collect(con, cal, members, session, bucket, min_turnover, before=before)
            if len(more) > len(events):
                note = "only %d past %s events%s; using the %s family (%s)" % (
                    len(events), etype, " in this bucket" if bucket else "", fam, ", ".join(members))
                events, used, types = more, fam, members
    out = {"type": etype, "used": used, "types": types, "session": session or "any", "bucket": bucket or "any",
           "cost": cost, "note": note, "summary": summarise(events, cal.market), "split": split_date(cal, before), "rules": {}}
    if len(events) < MIN_EVENTS:
        out["note"] = (note + "; " if note else "") + "too few events (%d < %d) for odds" % (len(events), MIN_EVENTS)
        return out, events
    if not entries:
        sessions = {e["session"] for e in events}
        entries = ("open0", "close0", "open1") if "during" not in sessions else ("close0", "open1")
    out["rules"]["best"] = best_rule(events, side, entries, cost, out["split"], sims, keep)
    if "close0" in entries:
        gap_rule = {"entry": "close0", "kind": "gap", "days": 1, "cost": cost}
        res = [x for x in (trade(e, gap_rule, side) for e in events) if x]
        if res:
            s = score(res)
            s["bootstrap"] = bootstrap([x for x, _ in res], sims)
            out["rules"]["overnight"] = {"rule": gap_rule, "text": describe_rule(gap_rule), "in_sample": s}
    return out, events


# ---------------------------------------------------------------- printing

def _p(x, nd=2):
    return "-" if x is None else ("%+.*f%%" % (nd, x))


def print_study(out, events, file=None):
    file = file or sys.stdout
    w = lambda s="": print(s, file=file)  # noqa: E731
    s = out["summary"]
    w("Event study: %s (session %s, day-0 move %s), %d past events, costs %.2f%% a round trip"
      % (out["used"], out["session"], out["bucket"], s["n"], out["cost"]))
    if out["note"]:
        w("  note: " + out["note"])
    if not s["n"]:
        w("  nothing to study: run `events.py backfill` first, or pick another type (`events.py types`).")
        return
    w("  %-26s %7s %9s %9s %7s" % ("", "n", "average", "median", "up"))
    for key, label in (("gap0", "day-0 open gap"), ("day0", "day-0 move"), ("runup5", "5 days before"),
                       ("overnight", "overnight (close -> open)"), ("day1", "day 1 close"), ("day5", "day 5 close"),
                       ("day10", "day 10 close"), ("day1_vs_market", "day 1 vs the market"),
                       ("day5_vs_market", "day 5 vs the market"), ("mfe10", "best point in 10 days"), ("mae10", "worst point in 10 days"),
                       ("to_meeting", "close before the meeting")):
        c = s[key]
        if c["n"]:
            w("  %-26s %7d %9s %9s %6.0f%%" % (label, c["n"], _p(c["mean"]), _p(c["median"]), c["up"]))
    if s["locked_up"]:
        w("  %d day-0 closes were locked at the upper circuit (not buyable at the close)." % s["locked_up"])
    buckets = {}
    for e in events:
        buckets.setdefault(e["bucket"], []).append(e)
    if out["bucket"] == "any" and len(buckets) > 1:
        w("  By day-0 move:      n   overnight   day 1    day 5")
        for _, _, name in BUCKETS:
            evs = buckets.get(name, [])
            if evs:
                b = summarise(evs)
                w("    %-14s %5d %10s %8s %8s" % (name, len(evs), _p(b["overnight"]["mean"]), _p(b["day1"]["mean"]),
                                                 _p(b["day5"]["mean"])))
    for key in ("overnight", "best"):
        r = out["rules"].get(key)
        if not r:
            continue
        i = r["in_sample"]
        b = i.get("bootstrap") or {}
        w("  %s: %s" % ("Overnight gap" if key == "overnight" else "Best rule (%s)" % r.get("side", "long"), r["text"]))
        w("    %d trades: average %s net, median %s, %0.f%% winners; 5-95%% of trades %s to %s"
          % (i["n"], _p(i["mean"]), _p(i["median"]), i["win"], _p(i["p5"]), _p(i["p95"])))
        if b:
            w("    %d bootstrap resamples: average %s to %s (5-95%%), winners %.0f-%.0f%%, edge above zero in %.0f%%"
              % (b["sims"], _p(b["mean_p5"]), _p(b["mean_p95"]), b["win_p5"], b["win_p95"], b["p_edge"]))
        if key == "best":
            w("    outcomes: %s; gapped through the stop %.1f%% of the time"
              % (", ".join("%s %.0f%%" % kv for kv in i["outcomes"].items()), i["gap_through"]))
            o = r.get("out_of_sample")
            w("    last %d months (out of sample): %s" % (OOS_MONTHS, "%d trades, average %s, %0.f%% winners"
                                                          % (o["n"], _p(o["mean"]), o["win"]) if o else "no trades"))
            if not r.get("oos_checked", True):
                w("    (under %d events before the last %d months: the rule was chosen on all of them)" % (MIN_EVENTS, OOS_MONTHS))
            w("    verdict: %s" % r["verdict"])
    w("  History, not a forecast. Sample sizes are on every line; an overnight hold can gap through any stop.")


# ---------------------------------------------------------------- suggestions

def classify_item(it):
    """A trader-news / trader-pelosi item (india_signals shape) -> event type or None."""
    if it["kind"] == "catalyst":
        if "band" in (it["source"] or "").lower():
            m = re.search(r"(\d+(?:\.\d+)?)%?\s*->\s*(\d+(?:\.\d+)?)", it["title"] + " " + it["detail"])
            if m and float(m.group(1)) != float(m.group(2)):
                return "band_cut" if float(m.group(2)) < float(m.group(1)) else "band_widen"
            return None
        desc = it["flags"][0] if it["flags"] else it["category"] or it["title"].split(":", 1)[-1].strip()
        return ic.event_type("%s %s" % (it["title"], it["detail"]), desc, it["company"])[0]
    flags = " ".join(it["flags"]).lower()
    cat = (it["category"] or "").lower()
    if "block deal" in flags:
        return "block_deal"
    if "bulk" in cat or "bulk deal" in flags:
        return "bulk_net_buy" if " bought " in it["title"] else "bulk_net_sell" if " sold " in it["title"] else None
    if "sast" in cat or "takeover" in cat:
        return "sast"
    if "insider" in cat or "pit" in cat:
        return "insider_trade"
    if "pledge" in cat:
        return "pledge_release" if ("released" in flags or "revoked" in flags) else "pledge_created"
    return None


def _symbol_atr(con, sym, n=14, before=None):
    rows = con.execute("SELECT high, low, prev_close, close, turnover_cr, date FROM bars WHERE symbol=? AND date<? "
                       "ORDER BY date DESC LIMIT 20", (sym, (before or "9999-12-31")[:10])).fetchall()
    if len(rows) < 5:
        return None, None, None, None
    trs = [(max(h, pc) - min(lo, pc)) / pc for h, lo, pc, _c, _t, _d in rows[:n]]
    return sum(trs) / len(trs), statistics.median([r[4] or 0 for r in rows]), rows[0][3], rows[0][5]


def next_session(day, cal):
    d = _dt.date.fromisoformat(day) + _dt.timedelta(days=1)
    while not is_trading_day(d, cal):
        d += _dt.timedelta(days=1)
    return d.isoformat()


def live_session(time_text, cal):
    """Like Calendar.session but for today and the days ahead, whose bars are not loaded yet."""
    day, hm = time_text[:10], time_text[11:16] or "00:00"
    if is_trading_day(_dt.date.fromisoformat(day), cal):
        if hm < OPEN_HM:
            return "pre", day
        if hm < CLOSE_HM:
            return "during", day
    return "after", next_session(day, cal)


def open_entries(session, day0, now, cal):
    """The entries still possible at `now` for an event whose day 0 is `day0`."""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    if stamp < day0 + " " + OPEN_HM:
        return ("open0", "close0", "open1") if session != "during" else ("close0", "open1")
    if stamp < day0 + " " + CLOSE_HM:
        return ("close0", "open1")
    if stamp < next_session(day0, cal) + " " + OPEN_HM:
        return ("open1",)
    return ()


def _entry_text(entry, session, day0, cal, side=1):
    verb = "buy" if side > 0 else "short (sell first)"
    if entry == "close0":
        return "%s 15:10-15:25 on %s, just before the close%s" % (
            verb, day0, " (to hold for the next morning's gap)" if session == "during" else "")
    if entry == "open0":
        return "%s at the open on %s (the filing came before that session)" % (verb, day0)
    return "%s at the open on %s" % (verb, next_session(day0, cal))


def intraday_rule(rule):
    return rule["entry"] == "open0" and rule["days"] == 1


def overnight_rule(rule):
    return not intraday_rule(rule)


def fno_info(con, symbol, day):
    """(lot size, banned?) for an F&O stock, (None, False) otherwise."""
    r = con.execute("SELECT lot FROM fno WHERE symbol=?", (symbol,)).fetchone()
    if not r:
        return None, False
    ban = con.execute("SELECT 1 FROM fno_ban WHERE symbol=? AND date>=?", (symbol, day)).fetchone()
    return r[0], bool(ban)


def suggest(con, now, items, quotes=None, cost=COST, min_turnover=MIN_TURNOVER_CR, sims=SIMS, max_ideas=15):
    """Ranked ideas from fresh items. `quotes` = {SYMBOL: {"price", "prev_close"}} (live day-0 move).
    Fresh = day 0 is today or the next session, and at least one entry is still possible."""
    cal = Calendar(con)
    quotes = quotes or {}
    stamp = now.strftime("%Y-%m-%d %H:%M")
    session_now = live_session(stamp, cal)[0]
    fresh, seen, skipped = [], set(), []
    for it in items:
        if not it["symbol"] or len(it["time"] or "") < 16:
            continue
        sess, day0 = live_session(it["time"][:16], cal)
        if (it["time"][:16] > stamp) or not open_entries(sess, day0, now, cal):
            continue
        et = classify_item(it)
        if not et or (it["symbol"], et) in seen:
            continue
        seen.add((it["symbol"], et))
        fresh.append((it, et, sess, day0))
    cache, ideas, warnings = {}, [], []
    for it, et, sess, day0 in fresh:
        sym = it["symbol"]
        entries = open_entries(sess, day0, now, cal)
        atr, turnover, last_close, last_day = _symbol_atr(con, sym, before=stamp)
        if atr is None:
            skipped.append("%s: no daily bars in the history (run `events.py update`)" % sym)
            continue
        if (turnover or 0) < min_turnover:
            skipped.append("%s: illiquid (median turnover Rs %.2f cr a day)" % (sym, turnover or 0))
            continue
        q = quotes.get(sym) or {}
        move = (q["price"] / q["prev_close"] - 1) * 100 if q.get("price") and q.get("prev_close") else None
        on_day0 = stamp[:10] == day0 and stamp >= day0 + " " + OPEN_HM
        bucket = bucket_of(move) if (move is not None and on_day0) else None
        def studied(side, ents, keep=None, label=""):
            key = (et, sess, bucket, ents, side, label)
            if key not in cache:
                st_ = study(con, et, sess, bucket, side, cost, min_turnover, sims, cal, entries=ents, keep=keep, before=stamp)[0]
                if bucket and not st_["rules"].get("best"):
                    st_ = study(con, et, sess, None, side, cost, min_turnover, sims, cal, entries=ents, keep=keep, before=stamp)[0]
                cache[key] = st_
            return cache[key]
        direction = ic.EVENT_DIRECTION.get(et, "?")
        st = studied(1, entries)
        best = st["rules"].get("best")
        base = {"symbol": sym, "type": et, "family": ic.family_of(et), "direction": direction, "event_time": it["time"],
                "title": it["title"], "link": it["link"], "session": sess, "day0": day0, "day0_move": move,
                "atr_pct": atr * 100, "turnover_cr": turnover}
        cands = []
        if best and best["verdict"].startswith("edge"):
            cands.append((st, best, 1, "cash"))
        leans_down = direction == "down" or (best and best["in_sample"]["mean"] < 0)
        lot, banned = fno_info(con, sym, day0)
        if leans_down or not cands:
            if "open0" in entries:
                s_ = studied(-1, ("open0",), intraday_rule, "intraday")
                b_ = s_["rules"].get("best")
                if b_ and b_["verdict"].startswith("edge"):
                    cands.append((s_, b_, -1, "cash intraday"))
            if lot and not banned:
                ents = tuple(e for e in entries)
                s_ = studied(-1, ents, overnight_rule, "futures")
                b_ = s_["rules"].get("best")
                if b_ and b_["verdict"].startswith("edge"):
                    cands.append((s_, b_, -1, "futures"))
            elif lot and banned:
                skipped.append("%s: in the F&O ban period, no new futures positions" % sym)
        if not cands:
            if not best:
                skipped.append("%s %s: %s" % (sym, et, st["note"] or "no rule with %d past trades" % MIN_EVENTS))
            elif leans_down:
                warnings.append(dict(base, why="history leans down after %s: the best long rule averaged %s (%s)%s" % (
                    st["used"], _p(best["in_sample"]["mean"]), best["verdict"],
                    "" if lot else "; not an F&O stock, so no overnight short")))
            else:
                skipped.append("%s %s: no edge after costs in %d past trades (%s)" % (
                    sym, et, best["in_sample"]["n"], best["verdict"]))
            continue
        st, best, side, instrument = max(cands, key=lambda c: c[1]["in_sample"]["bootstrap"]["mean_p5"])
        idea = dict(base, bucket=st["bucket"], studied=st["used"], samples=st["summary"]["n"], note=st["note"])
        i, o, rule = best["in_sample"], best["out_of_sample"], best["rule"]
        ref = q.get("price") or last_close
        stop = target = None
        if rule["kind"] == "bracket":
            stop = ref * (1 - side * rule["stop_atr"] * atr)
            target = ref * (1 + side * rule["target_r"] * rule["stop_atr"] * atr)
        entry_day = next_session(day0, cal) if rule["entry"] == "open1" else day0
        exit_day = day0     # path day k is the k-th session after day 0; open0's day 1 is day 0 itself
        for _ in range(rule["days"] - (1 if rule["entry"] == "open0" else 0)):
            exit_day = next_session(exit_day, cal)
        idea.update({"side": "long" if side > 0 else "short", "instrument": instrument,
                     "lot_size": lot if instrument == "futures" else None,
                     "rule": best["text"], "rule_spec": rule, "entry_day": entry_day, "exit_by": exit_day,
                     "entry": _entry_text(rule["entry"], sess, day0, cal, side),
                     "reference_price": ref, "stop": stop, "target": target,
                     "exit": "the next open" if rule["kind"] == "gap" else "the close of day %d" % rule["days"]
                     if exit_day != entry_day else "the close the same day (flat by 15:15)",
                     "average_net_pct": i["mean"], "median_net_pct": i["median"], "win_pct": i["win"],
                     "range_5_95": [i["p5"], i["p95"]], "outcomes": i["outcomes"], "gap_through_stop_pct": i["gap_through"],
                     "bootstrap": i.get("bootstrap"), "trades": i["n"],
                     "out_of_sample": {"n": o["n"], "mean": o["mean"], "win": o["win"]} if o else None,
                     "verdict": best["verdict"], "worst_5pct": i["p5"], "data_through": last_day})
        ideas.append(idea)
    ideas.sort(key=lambda d: (d["verdict"] == "edge", (d.get("bootstrap") or {}).get("mean_p5") or -99), reverse=True)
    return {"checked": stamp, "session": session_now, "fresh_items": len(fresh),
            "ideas": ideas[:max_ideas], "warnings": warnings, "skipped": skipped}


def is_trading_day(d, cal):
    """A session day: in the loaded bars, or a weekday that is not an NSE holiday."""
    return d.isoformat() in cal.set or (d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS)


def print_ideas(doc, file=None):
    file = file or sys.stdout
    w = lambda s="": print(s, file=file)  # noqa: E731
    w("Event ideas %s (now: %s; %d fresh items still tradeable)" % (doc["checked"], doc["session"], doc["fresh_items"]))
    if not doc["ideas"]:
        w("  No idea passed: none of today's filings has a past record with an edge after costs.")
    for n, d in enumerate(doc["ideas"], 1):
        b = d.get("bootstrap") or {}
        w("%d. %s  %s  %s%s  (%s, %s)" % (n, d["symbol"], d["type"], d.get("side", "long").upper(),
                                          " " + d["instrument"] if d.get("instrument", "cash") != "cash" else "",
                                          d["verdict"], d["event_time"]))
        w("   %s" % d["title"][:120])
        w("   %s: %s" % (d["entry"], d["rule"]))
        lv = "   reference Rs %.2f" % d["reference_price"] if d.get("reference_price") else "   "
        if d.get("stop"):
            lv += ", stop Rs %.2f, target Rs %.2f" % (d["stop"], d["target"])
        w(lv + ", exit %s" % d["exit"])
        w("   past %d trades (%s%s): average %s net, %0.f%% winners, 5-95%% %s to %s; edge above zero in %.0f%% of %d resamples"
          % (d["trades"], d["studied"], ", day-0 " + d["bucket"] if d["bucket"] != "any" else "", _p(d["average_net_pct"]),
             d["win_pct"], _p(d["range_5_95"][0]), _p(d["range_5_95"][1]), b.get("p_edge", 0), b.get("sims", 0)))
        o = d.get("out_of_sample")
        if o:
            w("   last %d months: %d trades, average %s" % (OOS_MONTHS, o["n"], _p(o["mean"])))
        if d.get("note"):
            w("   note: %s" % d["note"])
        w("   %s" % d["link"])
    for d in doc["warnings"]:
        w("! %s %s: %s  %s" % (d["symbol"], d["type"], d["why"], d["link"]))
    if doc["skipped"]:
        w("Skipped %d: %s" % (len(doc["skipped"]), "; ".join(doc["skipped"][:8]) + (" ..." if len(doc["skipped"]) > 8 else "")))
    w("Suggestions only: history, not a forecast. Check /trader-exit, /trader-budget and /trader-risk before any trade.")


# ---------------------------------------------------------------- CLI

def _client(args):
    return ih.Client(offline=getattr(args, "offline", None), record=getattr(args, "record", None))


def cmd_backfill(args, con):
    today = ih.now_ist().date()
    end = _dt.date.fromisoformat(args.until) if args.until else today
    if args.since:
        start = _dt.date.fromisoformat(args.since)
    else:
        start = end - _dt.timedelta(days=int(args.years * 365.25))
    sources = tuple(args.only.split(",")) if args.only else SOURCES
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        print("unknown source %s (use %s)" % (", ".join(bad), ",".join(SOURCES)), file=sys.stderr)
        return 2
    print("Backfill %s .. %s (%s) into %s" % (start, end, ",".join(sources), args.db))
    stats = backfill(con, _client(args), start, end, sources, today=today)
    print("Done: %(days)d trading days (%(bars)d bars), %(holidays)d holidays, %(events)d events, %(calls)d requests, "
          "%(errors)d errors" % stats)
    return 0


def cmd_update(args, con):
    last = last_loaded(con, "bars")
    today = ih.now_ist().date()
    start = (_dt.date.fromisoformat(last) - _dt.timedelta(days=7)) if last else today - _dt.timedelta(days=30)
    stats = backfill(con, _client(args), start, today, SOURCES, today=today)
    print("Updated from %s: %d new trading days, %d events" % (start, stats["days"], stats["events"]))
    return 0


def cmd_types(args, con):
    rows = con.execute("SELECT type, count(*), min(time), max(time) FROM events GROUP BY type ORDER BY 2 DESC").fetchall()
    nb = con.execute("SELECT count(*), min(date), max(date) FROM days WHERE n>0").fetchone()
    print("Daily bars: %d sessions (%s to %s)" % (nb[0], nb[1], nb[2]))
    for t, n, a, b in rows:
        print("  %-26s %7d  %s .. %s  %s, family %s" % (t, n, a[:10], b[:10], ic.EVENT_DIRECTION.get(t, "?"), ic.family_of(t)))
    if not rows:
        print("  no events yet: run `events.py backfill`")
    return 0


def cmd_study(args, con):
    if args.type not in ic.ALL_TYPES:
        print("unknown type %s; types: %s" % (args.type, ", ".join(ic.ALL_TYPES)), file=sys.stderr)
        return 2
    side = -1 if args.short else 1
    before = ih.now_ist().strftime("%Y-%m-%d %H:%M") if args.now else None
    out, events = study(con, args.type, args.session, args.bucket, side, args.cost, args.min_turnover, args.sims,
                        before=before)
    if args.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        print_study(out, events)
    return 0


def cmd_suggest(args, con):
    import india_signals as isg
    now = ih.now_ist()
    items = isg.load_all(args.base)
    quotes = {}
    if args.quotes:
        quotes = ih.load_json(args.quotes) or {}
    elif not args.no_quotes and not args.offline:
        syms = sorted({i["symbol"] for i in items if i["symbol"] and (i["time"] or "") >= now.strftime("%Y-%m-%d")})
        if syms:
            try:
                import nse_data
                got, _errors = nse_data.fetch_quotes(syms[:args.max_quotes])
                quotes = {s: q for s, q in got.items() if isinstance(q, dict)}
            except Exception as e:
                print("  (live quotes unavailable: %s; using the last close)" % str(e)[:120], file=sys.stderr)
    doc = suggest(con, now, items, quotes, args.cost, args.min_turnover, args.sims, args.max)
    if args.json:
        print(json.dumps(doc, indent=1, default=str))
    else:
        print_ideas(doc)
    if not args.dry_run:
        ih.write_json(os.path.join(args.out, "latest.json"), doc)
        rows = [{"checked": doc["checked"], "symbol": d["symbol"], "type": d["type"], "verdict": d["verdict"],
                 "rule": d["rule"], "reference_price": d.get("reference_price"), "stop": d.get("stop"),
                 "target": d.get("target"), "average_net_pct": round(d["average_net_pct"], 3), "win_pct": round(d["win_pct"], 1),
                 "trades": d["trades"], "link": d["link"]} for d in doc["ideas"]]
        if rows:
            ih.append_csv(os.path.join(args.out, "ideas.csv"), rows, list(rows[0]))
    return 0 if doc["ideas"] else 1


def plan_from_idea(idea, min_rr=1.0, now=None):
    """An idea from `suggest` -> a trader-exit style swing plan (trader/plan.json) for budget, risk, alerts.
    A time-exit rule (overnight gap, plain hold) still gets a stop: the larger of 1 ATR and the worst-5 %
    loss, and a target at min_reward_risk x that risk, marked as a ceiling."""
    ref = float(idea["reference_price"])
    rule = idea["rule_spec"]
    sgn = -1 if idea.get("side") == "short" else 1
    where = "under" if sgn > 0 else "over"
    gap = max(0.0, -(idea.get("worst_5pct") or 0.0))
    if idea.get("stop") and idea.get("target"):
        stop, target = float(idea["stop"]), float(idea["target"])
        stop_label = "%g x ATR %s the entry (event rule)" % (rule["stop_atr"], where)
        target_label = "%g x risk (event rule)" % rule["target_r"]
    else:
        dist = max(idea["atr_pct"], gap) / 100.0
        stop = ref * (1 - sgn * dist)
        target = ref * (1 + sgn * max(min_rr, 1.0) * dist)
        stop_label = "max(1 ATR, the worst-5%% loss %.2f%%) %s the entry" % (gap, where)
        target_label = "a ceiling only: the rule exits on time (%s)" % idea["exit"]
    stop = (math.floor(stop * 100) if sgn > 0 else math.ceil(stop * 100 - 1e-6)) / 100.0
    risk = round(abs(ref - stop), 2)
    if not (idea.get("stop") and idea.get("target")):
        # a ceiling at min_rr x the rounded risk, rounded away from the entry so reward:risk never dips under it
        target = ref + sgn * max(min_rr, 1.0) * risk
        target = (math.ceil(target * 100 - 1e-6) if sgn > 0 else math.floor(target * 100 + 1e-6)) / 100.0
    else:
        target = math.floor(target * 100 + 0.5) / 100.0
    hold = 0
    d = idea["entry_day"]
    while d < idea["exit_by"]:
        d = (_dt.date.fromisoformat(d) + _dt.timedelta(days=1)).isoformat()
        if d not in NSE_HOLIDAYS and _dt.date.fromisoformat(d).weekday() < 5:
            hold += 1
    when = {"close0": "3:15 PM", "open0": "9:15 AM", "open1": "9:15 AM"}[rule["entry"]]
    extra = {}
    if sgn < 0:
        extra["short"] = True
    if idea.get("instrument") == "futures":
        extra.update({"instrument": "futures", "lot_size": int(idea["lot_size"])})
    return dict(extra, **{"created_by": "trader-timemachine events.py", "symbol": idea["symbol"],
            "side": "buy" if sgn > 0 else "sell",
            "date": idea["entry_day"], "setup": "event", "entry": round(ref, 2), "entry_time": when,
            "entry_bar": None, "interval": "5m", "opening_range": None, "stop": stop, "stop_rule": "event",
            "stop_label": stop_label, "target": target, "target_r": round(abs(target - ref) / risk, 2) if risk else None,
            "target_label": target_label, "risk_per_share": risk,
            "reward_risk": round(abs(target - ref) / risk, 2) if risk else None, "flat_by": None if hold else "15:15",
            "mode": "swing" if hold else "day", "exit_by": idea["exit_by"] if hold else None, "hold_sessions": hold,
            "gap_risk_pct": round(gap, 2) if gap else None,
            "source": "%s %s %s (%s)" % (idea["symbol"], idea["type"], idea["event_time"], idea["link"]),
            "event": {"rule": idea["rule"], "entry": idea["entry"], "exit": idea["exit"], "trades": idea["trades"],
                      "average_net_pct": idea["average_net_pct"], "win_pct": idea["win_pct"],
                      "verdict": idea["verdict"]},
            "created_at": (now or ih.now_ist()).strftime("%Y-%m-%d %H:%M IST"),
            "note": "An event idea, history not a forecast. A stop is a plan, not a guarantee: an overnight gap "
                    "can open past it."})


def cmd_plan(args, con):
    doc = ih.load_json(args.ideas) or {}
    ideas = doc.get("ideas") or []
    pick = None
    if args.idea.isdigit() and 1 <= int(args.idea) <= len(ideas):
        pick = ideas[int(args.idea) - 1]
    else:
        pick = next((d for d in ideas if d["symbol"] == args.idea.upper()), None)
    if not pick:
        print("no idea %r in %s (run `events.py suggest` first)" % (args.idea, args.ideas), file=sys.stderr)
        return 1
    cfg = ih.load_json(args.config) or {}
    plan = plan_from_idea(pick, float(cfg.get("min_reward_risk") or 1.0))
    print("%s plan from idea %s: %s %s%s, %s" % (
        "Swing" if plan["mode"] == "swing" else "Day", args.idea, plan["symbol"], "SHORT" if plan.get("short") else "long",
        " (futures, lot %d)" % plan["lot_size"] if plan.get("instrument") == "futures" else "", pick["rule"]))
    print("  Entry   Rs %.2f, %s" % (plan["entry"], pick["entry"]))
    print("  Stop    Rs %.2f  %s" % (plan["stop"], plan["stop_label"]))
    print("  Target  Rs %.2f  %s" % (plan["target"], plan["target_label"]))
    if plan["mode"] == "swing":
        print("  Exit by the close of %s (%d session%s held overnight)%s" % (
            plan["exit_by"], plan["hold_sessions"], "" if plan["hold_sessions"] == 1 else "s",
            "; worst 5%% of past trades lost %.2f%% or more" % plan["gap_risk_pct"] if plan["gap_risk_pct"] else ""))
    else:
        print("  Out the same day, flat by 15:15 (a day trade)")
    if args.write:
        ih.write_json(args.write, plan)
        print("Wrote %s." % args.write)
    print("Next: /trader-budget sizes it on the stop and the gap risk, /trader-risk checks it (swing trades need "
          "\"swing\": {\"allow_overnight\": true}), /trader-alerts add --from-plan. Nothing was placed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--now", help=argparse.SUPPRESS)
    ap.add_argument("--offline", help=argparse.SUPPRESS)
    ap.add_argument("--record", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backfill", help="fill the history (resumable)")
    b.add_argument("--years", type=float, default=5)
    b.add_argument("--since")
    b.add_argument("--until")
    b.add_argument("--only", help="comma list of " + ",".join(SOURCES))
    sub.add_parser("update", help="the days since the last backfill")
    sub.add_parser("types", help="event types in the history")
    s = sub.add_parser("study", help="what followed past events of one type")
    s.add_argument("--type", required=True)
    s.add_argument("--session", choices=("pre", "during", "after"))
    s.add_argument("--bucket", choices=[b_[2] for b_ in BUCKETS])
    s.add_argument("--short", action="store_true", help="study the short side (ideas stay long-only for now)")
    s.add_argument("--json", action="store_true")
    g = sub.add_parser("suggest", help="ideas from today's trader-news / trader-pelosi items")
    g.add_argument("--base", default=BASE)
    g.add_argument("--out", default=IDEAS_DIR)
    g.add_argument("--quotes", help=argparse.SUPPRESS)
    g.add_argument("--no-quotes", action="store_true", help="skip NSE live quotes (use the last close)")
    g.add_argument("--max-quotes", type=int, default=12)
    g.add_argument("--max", type=int, default=15)
    g.add_argument("--json", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    pl = sub.add_parser("plan", help="turn idea N (or a symbol) from `suggest` into trader/plan.json (swing)")
    pl.add_argument("idea")
    pl.add_argument("--ideas", default=os.path.join(IDEAS_DIR, "latest.json"))
    pl.add_argument("--config", default=os.path.join("trader", "config.json"))
    pl.add_argument("--write", nargs="?", const=os.path.join("trader", "plan.json"), metavar="PATH")
    for p in (s, g):
        p.add_argument("--cost", type=float, default=COST, help="round-trip cost in %% (default 0.30)")
        p.add_argument("--min-turnover", type=float, default=MIN_TURNOVER_CR, help="Rs cr a day (default 1)")
        p.add_argument("--sims", type=int, default=SIMS)
    args = ap.parse_args(argv)
    if args.now:
        ih.pin_clock(args.now)
    if not args.cmd:
        ap.print_help()
        return 2
    con = connect(args.db)
    try:
        return {"backfill": cmd_backfill, "update": cmd_update, "types": cmd_types, "study": cmd_study,
                "suggest": cmd_suggest, "plan": cmd_plan}[args.cmd](args, con)
    except KeyboardInterrupt:
        con.commit()
        print("\nstopped; run the same command again to resume", file=sys.stderr)
        return 130
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
