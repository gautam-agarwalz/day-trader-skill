"""NSE market facts the catalyst engine needs, from NSE's own files and APIs (no paid data, no AI):

    market_cap_cr(client, symbol)   market cap in Rs crore: NSE's quote API (issued shares x last
                                    price), else trader/market_caps.json ({"KEC": 7600}), else None
    fno_oi(client, day)             every stock future's open interest and its change, from NSE's
                                    daily F&O bhavcopy (BhavCopy_NSE_FO_..._<YYYYMMDD>_F_0000.csv.zip)
    oi_buildup(row)                 "long build-up" / "short build-up" / "short covering" / "long unwinding"
    fno_lots(client), fno_ban(client)   NSE's lot sizes (fo_mktlots.csv) and the F&O ban list (fo_secban.csv)
    equity_master(client)           {SYMBOL: company name} from NSE's EQUITY_L.csv
    NameMatcher(master)             finds listed companies named in a headline
    route(side, ...)                the equity-first routing tags (SEBI: a cash short is intraday only)

Answers are cached for the day in trader/cache/market.json, so a 60-second loop asks NSE once.
The same copy lives in trader-news, trader-scanner and trader-timemachine (a repo test keeps them identical).
"""

import csv
import datetime as _dt
import io
import os
import re
import zipfile

import india_http as ih
import india_calendar

ARCH = "https://nsearchives.nseindia.com"
EQUITY_L = ARCH + "/content/equities/EQUITY_L.csv"
FO_LOTS = ARCH + "/content/fo/fo_mktlots.csv"
FO_BAN = ARCH + "/content/fo/fo_secban.csv"
FO_BHAV = ARCH + "/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip"
CACHE = os.path.join("trader", "cache", "market.json")
USER_MCAPS = os.path.join("trader", "market_caps.json")
CRORE = 1e7


# ---------------------------------------------------------------- the session (mirrors trader-scanner/scan.py)

NSE_HOLIDAYS = india_calendar.NSE_HOLIDAYS  # NSE trading holidays: built-in 2026 + NSE's API + trader/nse_holidays.json


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS


def market_hours(now=None, start="09:00", end="15:45"):
    """True on a trading day between start and end, India time (the fast-polling window)."""
    now = now or ih.now_ist()
    return trading_day(now.date()) and start <= now.strftime("%H:%M") <= end


# ---------------------------------------------------------------- the day cache

def _cache(path=CACHE):
    doc = ih.load_json(path) or {}
    today = ih.now_ist().strftime("%Y-%m-%d")
    if doc.get("day") != today:
        keep = {"mcap": {k: v for k, v in (doc.get("mcap") or {}).items()
                         if v.get("day", "") >= (ih.now_ist() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")}}
        doc = {"day": today, **keep}
    return doc


def _save(doc, path=CACHE):
    try:
        ih.write_json(path, doc)
    except OSError:
        pass


# ---------------------------------------------------------------- market cap

def parse_quote_equity(doc):
    """NSE /api/quote-equity answer -> (market cap in Rs crore, last price) or (None, None)."""
    info = (doc or {}).get("securityInfo") or {}
    price = ih.num(((doc or {}).get("priceInfo") or {}).get("lastPrice"))
    shares = ih.num(info.get("issuedSize"))
    if not price or not shares:
        return None, price
    return round(shares * price / CRORE, 2), price


SHP_MASTER = "/api/corporate-share-holdings-master?index=equities&symbol=%s"
BHAV = ARCH + "/products/content/sec_bhavdata_full_%s.csv"
SHARES_KEEP_DAYS = 100        # a shareholding pattern is filed every quarter


def parse_shp_shares(xml):
    """A shareholding-pattern XBRL -> total fully paid-up equity shares (the ShareholdingPattern_ContextI
    total), or None."""
    vals = [ih.num(v) for v in re.findall(r"<in-bse-shp:NumberOfFullyPaidUpEquityShares[^>]*contextRef=\""
                                         r"ShareholdingPattern_ContextI\"[^>]*>([^<]+)<", xml or "")]
    vals = [v for v in vals if v]
    return max(vals) if vals else None


def shares_outstanding(client, symbol, cache_path=CACHE):
    """Equity shares from the company's latest shareholding filing (NSE's master list -> its XBRL)."""
    doc = _cache(cache_path)
    hit = (doc.get("shares") or {}).get(symbol)
    old = (ih.now_ist() - _dt.timedelta(days=SHARES_KEEP_DAYS)).strftime("%Y-%m-%d")
    if hit and hit.get("day", "") >= old:
        return hit.get("n")
    rows = client.nse_api(SHP_MASTER % symbol, "nse_shp_%s.json" % symbol) or []
    rows = [r for r in rows if isinstance(r, dict) and r.get("xbrl")]
    n = None
    if rows:
        n = parse_shp_shares(client.get_text(rows[0]["xbrl"], "shp_%s.xml" % symbol))
    doc.setdefault("shares", {})[symbol] = {"n": n, "day": ih.now_ist().strftime("%Y-%m-%d"),
                                           "as_of": rows[0].get("date") if rows else None}
    _save(doc, cache_path)
    return n


def parse_bhav(text):
    """sec_bhavdata_full CSV -> {SYMBOL: {close, turnover_cr, series, deliv_pct}} for series EQ / BE
    (BE is trade-for-trade: every trade settles by delivery, no intraday, no short selling)."""
    out = {}
    rd = csv.reader(io.StringIO((text or "").replace("\r", "")))
    head = [h.strip().upper() for h in next(rd, [])]
    if "SYMBOL" not in head or "CLOSE_PRICE" not in head:
        return out
    ix = {h: i for i, h in enumerate(head)}
    for r in rd:
        if len(r) <= ix["CLOSE_PRICE"] or r[ix["SERIES"]].strip() not in ("EQ", "BE"):
            continue
        close = ih.num(r[ix["CLOSE_PRICE"]])
        if not close:
            continue
        lacs = ih.num(r[ix["TURNOVER_LACS"]]) if "TURNOVER_LACS" in ix else None
        out[r[ix["SYMBOL"]].strip().upper()] = {
            "close": close, "series": r[ix["SERIES"]].strip(),
            "turnover_cr": round(lacs / 100.0, 2) if lacs is not None else None,
            "deliv_pct": ih.num(r[ix["DELIV_PER"]]) if "DELIV_PER" in ix else None}
    return out


def parse_bhav_closes(text):
    """sec_bhavdata_full CSV -> {SYMBOL: close} for series EQ / BE."""
    return {k: v["close"] for k, v in parse_bhav(text).items()}


def last_bhav(client, cache_path=CACHE, back=7):
    """The latest bhavcopy, {SYMBOL: {close, turnover_cr, series, deliv_pct}} (cached for the day), and its date."""
    doc = _cache(cache_path)
    if doc.get("bhav"):
        return doc["bhav"], doc.get("bhav_day")
    now = ih.now_ist()
    d = now.date() - _dt.timedelta(days=0 if now.strftime("%H:%M") >= "18:30" else 1)   # today's file after 18:30
    for k in range(back):
        if k:
            d -= _dt.timedelta(days=1)
        try:
            got = parse_bhav(client.get_text(BHAV % d.strftime("%d%m%Y"), "bhav_%s.csv" % d.strftime("%d%m%Y")))
        except ih.FeedError:
            continue
        if got:
            doc["bhav"], doc["bhav_day"] = got, d.isoformat()
            doc["closes"], doc["closes_day"] = {k_: v["close"] for k_, v in got.items()}, d.isoformat()
            _save(doc, cache_path)
            return got, d.isoformat()
    return {}, None


def last_closes(client, cache_path=CACHE, back=7):
    """Every stock's last close from the latest bhavcopy (cached for the day)."""
    doc = _cache(cache_path)
    if doc.get("closes"):
        return doc["closes"]
    got, _ = last_bhav(client, cache_path, back)
    return {k: v["close"] for k, v in got.items()}


def market_cap_cr(client, symbol, cache_path=CACHE, user_path=USER_MCAPS, notes=None):
    """Rs crore, or None when no source knows it (the caller says so instead of guessing). In order:
    trader/market_caps.json, NSE's quote API (issued shares x last price), then the shares in the latest
    shareholding filing x the last bhavcopy close (when the quote API refuses the network)."""
    sym = str(symbol or "").upper()
    if not sym:
        return None
    user = ih.load_json(user_path) or {}
    if sym in user and ih.num(user[sym]):
        return float(ih.num(user[sym]))
    doc = _cache(cache_path)
    hit = (doc.get("mcap") or {}).get(sym)
    if hit and hit.get("cr"):
        return hit["cr"]
    if hit and hit.get("day") == doc["day"] and hit.get("failed"):
        return None
    cr, how = None, None
    if not doc.get("quote_api_down"):
        try:
            cr, _ = parse_quote_equity(client.nse_api("/api/quote-equity?symbol=%s" % sym, "nse_quote_%s.json" % sym))
            how = "NSE quote"
        except ih.FeedError as e:
            doc["quote_api_down"] = str(e)[:120]          # one refusal is enough for the day
    if cr is None:
        try:
            n = shares_outstanding(client, sym, cache_path)
            px = last_closes(client, cache_path).get(sym)
            if n and px:
                cr, how = round(n * px / CRORE, 2), "shareholding filing x last close"
        except ih.FeedError as e:
            if notes is not None:
                notes.append("market cap %s: %s" % (sym, str(e)[:100]))
        doc = dict(_cache(cache_path), quote_api_down=doc.get("quote_api_down"))
    if cr is None and notes is not None and not any("market cap:" in n for n in notes):
        notes.append("market cap: no source answered (%s); add caps to %s" % (doc.get("quote_api_down") or "-", user_path))
    doc.setdefault("mcap", {})[sym] = {"cr": cr, "day": doc["day"], "failed": cr is None, "how": how}
    _save(doc, cache_path)
    return cr


# ---------------------------------------------------------------- F&O

def parse_lots(text):
    """NSE fo_mktlots.csv -> {SYMBOL: lot size of the nearest expiry}."""
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
    """NSE fo_secban.csv -> (YYYY-MM-DD trade date, [SYMBOL])."""
    day = None
    m = re.search(r"Trade Date\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", text or "")
    if m:
        day = _dt.datetime.strptime(m.group(1).title(), "%d-%b-%Y").date().isoformat()
    syms = [ln.split(",", 1)[1].strip().upper() for ln in (text or "").splitlines()
            if re.match(r"^\s*\d+\s*,", ln) and len(ln.split(",", 1)) == 2]
    return day, [x for x in syms if x]


def fno_lots(client, cache_path=CACHE):
    doc = _cache(cache_path)
    if doc.get("lots"):
        return doc["lots"]
    lots = parse_lots(client.get_text(FO_LOTS, "fo_mktlots.csv"))
    if lots:
        doc["lots"] = lots
        _save(doc, cache_path)
    return lots


def fno_ban(client, cache_path=CACHE):
    doc = _cache(cache_path)
    if "ban" in doc:
        return doc["ban"]
    day, syms = parse_ban(client.get_text(FO_BAN, "fo_secban.csv"))
    doc["ban"] = {"day": day, "symbols": syms}
    _save(doc, cache_path)
    return doc["ban"]


def parse_fo_bhav(data):
    """The F&O UDiFF bhavcopy (zip bytes or CSV text) -> {SYMBOL: {oi, oi_change, close, prev_close,
    oi_change_pct, contracts}} summed over the stock's futures (FinInstrmTp STF)."""
    if isinstance(data, bytes) and data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        data = z.read(z.namelist()[0]).decode("utf-8", "replace")
    elif isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    out = {}
    near = {}
    for r in csv.DictReader(io.StringIO(data)):
        if (r.get("FinInstrmTp") or "").strip() != "STF":
            continue
        sym = (r.get("TckrSymb") or "").strip().upper()
        oi, chg = ih.num(r.get("OpnIntrst")) or 0.0, ih.num(r.get("ChngInOpnIntrst")) or 0.0
        o = out.setdefault(sym, {"oi": 0.0, "oi_change": 0.0, "contracts": 0, "close": None, "prev_close": None,
                                 "day": (r.get("TradDt") or "").strip()})
        o["oi"] += oi
        o["oi_change"] += chg
        o["contracts"] += 1
        exp = (r.get("XpryDt") or "").strip()
        if sym not in near or exp < near[sym]:    # the near month carries the price
            near[sym] = exp
            o["close"], o["prev_close"] = ih.num(r.get("ClsPric")), ih.num(r.get("PrvsClsgPric"))
    for o in out.values():
        base = o["oi"] - o["oi_change"]
        o["oi_change_pct"] = round(o["oi_change"] / base * 100, 2) if base > 0 else None
    return out


def fno_oi(client, day=None, back=6, notes=None):
    """The latest F&O bhavcopy on or before `day` (a date): ({SYMBOL: row}, its date) or ({}, None)."""
    d = day or ih.now_ist().date()
    for _ in range(back):
        stamp = d.strftime("%Y%m%d")
        try:
            return parse_fo_bhav(client.get_bytes(FO_BHAV % stamp, "fo_bhav_%s.csv.zip" % stamp)), d.isoformat()
        except ih.FeedError:
            d -= _dt.timedelta(days=1)
        except (zipfile.BadZipFile, KeyError, ValueError) as e:
            if notes is not None:
                notes.append("F&O bhavcopy %s unreadable: %s" % (stamp, e))
            d -= _dt.timedelta(days=1)
    if notes is not None:
        notes.append("F&O bhavcopy: none found in the last %d days" % back)
    return {}, None


def oi_buildup(row):
    """Price and open interest together: the standard reading of a day's futures move."""
    if not row or row.get("close") is None or not row.get("prev_close"):
        return None
    up = row["close"] > row["prev_close"]
    oi_up = (row.get("oi_change") or 0) > 0
    return {(True, True): "long build-up", (False, True): "short build-up",
            (True, False): "short covering", (False, False): "long unwinding"}[(up, oi_up)]


# ---------------------------------------------------------------- companies

def parse_equity_list(text):
    out = {}
    for r in csv.reader(io.StringIO((text or "").replace("\r", ""))):
        if len(r) >= 2 and r[0].strip() and r[0].strip().upper() != "SYMBOL":
            out[r[0].strip().upper()] = r[1].strip()
    return out


def equity_master(client, cache_path=CACHE):
    doc = _cache(cache_path)
    if doc.get("master"):
        return doc["master"]
    master = parse_equity_list(client.get_text(EQUITY_L, "EQUITY_L.csv"))
    if master:
        doc["master"] = master
        _save(doc, cache_path)
    return master


SUFFIX = re.compile(r"\b(?:limited|ltd|pvt|private|the|co|company|corporation|corp|inc|plc)\b\.?", re.I)
GENERIC = {"india", "indian", "national", "bharat", "hindustan", "global", "power", "steel", "energy", "infra",
           "infrastructure", "engineering", "industries", "projects", "systems", "solutions", "technologies",
           "capital", "finance", "bank", "railway", "railways", "defence", "electricals", "electronics", "metals",
           "cement", "chemicals", "motors", "textiles", "holdings", "enterprises", "international", "construction",
           "group", "ventures", "services", "transport", "logistics", "solar", "green", "water", "gas", "oil"}


def norm_name(text):
    t = SUFFIX.sub(" ", (text or "").lower().replace("&", " and "))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class NameMatcher(object):
    """Listed company names in free text. A name counts only when its normalised form appears as whole
    words; single generic words ("India", "Power") never count on their own. Aliases (short names the
    press uses: "RVNL", "L&T") come from trader/company_aliases.json {"ALIAS": "SYMBOL"} plus the
    symbols themselves when written in capitals and at least 4 letters long."""

    def __init__(self, master, aliases=None):
        self.names = {}
        for sym, name in (master or {}).items():
            n = norm_name(name)
            words = n.split()
            if not words or (len(words) == 1 and (words[0] in GENERIC or len(words[0]) < 5)):
                continue
            self.names[n] = sym
            short = re.sub(r"(?: of)? india$", "", n).strip()      # "power grid of india" -> "power grid"
            if short != n and len(short.split()) >= 2:
                self.names.setdefault(short, sym)
        self.aliases = {k.upper(): v.upper() for k, v in (aliases or {}).items()}
        self.symbols = set(master or {})
        self._rx = None

    def find(self, text):
        """[(symbol, how it matched)], longest name first, one per symbol."""
        n = " %s " % norm_name(text)
        found, seen = [], set()
        for name in sorted(self.names, key=len, reverse=True):
            if " %s " % name in n:
                sym = self.names[name]
                if sym not in seen:
                    seen.add(sym)
                    found.append((sym, "name"))
                n = n.replace(" %s " % name, " ")      # "Bharat Electronics" is not also "Electronics ..."
        for tok in set(re.findall(r"\b[A-Z][A-Z&]{2,14}\b", text or "")):
            sym = self.aliases.get(tok) or (tok if tok in self.symbols and len(tok) >= 4 else None)
            if sym and sym not in seen:
                seen.add(sym)
                found.append((sym, "symbol"))
        return found


# ---------------------------------------------------------------- equity-first routing (SEBI cash rules)

TAG_INTRADAY_LONG = "[EQUITY INTRADAY LONG]"
TAG_SWING_LONG = "[EQUITY SWING LONG: 2-3+ DAYS]"
TAG_INTRADAY_SHORT = "[EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM]"
TAG_FNO_SHORT = "[SECONDARY F&O SWING SHORT OPTION]"


def route(side, swing=False, fno_listed=False, fno_banned=False, multi_day_bearish=False):
    """Routing tags, cash equity first. A long can be intraday or a 2-3+ day delivery swing. A cash short
    in India must be squared off the same day, so a short is always intraday in cash; the F&O swing short
    is added only for an F&O stock, not in the ban period, with a multi-day bearish setup."""
    if side == "long":
        return [TAG_SWING_LONG] if swing else [TAG_INTRADAY_LONG]
    tags = [TAG_INTRADAY_SHORT]
    if fno_listed and not fno_banned and multi_day_bearish:
        tags.append(TAG_FNO_SHORT)
    return tags
