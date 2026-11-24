"""Candles for the order block scanner, the macro engine's volume gate and the brief's Time Machine.

    fetch_text(sym, interval, offline=DIR, record=DIR)   Yahoo's chart JSON: "5m" (60 days) or "60m" (about
                                                          2 years); macro proxies too (GC=F, CL=F, ^VIX)
    parse(text, now_utc)       -> [{"date", "time", "ts", "o", "h", "l", "c", "v"}] in India time; NSE stocks
                                  keep the 9:15-15:30 session and complete bars only
    resample(bars, 15)         5-min bars -> 15-min bars, per session
    nse_daily(symbol, months)  daily bars from NSE's bhavcopy MCP server (nse_data.fetch_daily)
    atr, vwap, session_vwap, anchored_vwap, vol_ratio, ema   the measures the scanners use

NSE's MCP servers have no intraday candles, so intraday comes from Yahoo (free, unofficial, can lag:
query2 first, query1 on a 429 or 5xx, a polite pause). No language model, no paid data.
The same copy lives in trader-scanner and trader-timemachine (a repo test keeps them identical).
"""

import datetime as _dt
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
RANGES = {"5m": "60d", "15m": "60d", "60m": "730d", "1d": "2y"}
IST = _dt.timedelta(hours=5, minutes=30)
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
PAUSE = 0.4
STEP = {"5m": 5, "15m": 15, "60m": 60, "1d": 1440}


class BarsError(Exception):
    pass


def _utc(ts):
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).replace(tzinfo=None)


def yahoo_symbol(sym):
    """RELIANCE -> RELIANCE.NS; futures and indices (GC=F, ^VIX, DX-Y.NYB) and .NS / .BO stay as they are."""
    s = str(sym or "").strip().upper()
    if any(ch in s for ch in "=^") or s.endswith((".NS", ".BO", ".NYB")):
        return s
    return s + ".NS"


def is_nse(sym):
    return yahoo_symbol(sym).endswith(".NS")


def chart_url(sym, interval="5m", rng=None, host=HOSTS[0]):
    return "%s/v8/finance/chart/%s?range=%s&interval=%s" % (
        host, urllib.parse.quote(yahoo_symbol(sym), safe=".-^="), rng or RANGES.get(interval, "60d"), interval)


def offline_name(sym, interval):
    return "%s_%s.json" % (yahoo_symbol(sym).replace("^", "_").replace("=", "_"), interval)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


LAST_SOURCE = {}       # symbol -> where its last chart came from ("angelone" / "yahoo" / "offline")


def fetch_text(sym, interval="5m", rng=None, offline=None, record=None, pause=PAUSE):
    """The chart JSON text. For NSE stocks Angel One SmartAPI comes first when its ANGEL_* environment
    variables are set (about 90 days of 5-minute bars, against Yahoo's 60); Yahoo is the fallback and the
    only source for global proxies (GC=F, ^VIX). TRADER_INTRADAY=yahoo / angel forces one.
    offline=DIR reads DIR/<SYM.NS>_<interval>.json(.gz) (or the time machine's <SYM>-60d5m.json(.gz) for 5m);
    record=DIR saves what the network answered."""
    if not offline and is_nse(sym) and interval in ("5m", "15m", "60m", "1d"):
        try:
            import angel_one
        except ImportError:
            angel_one = None
        prov = angel_one.provider() if angel_one else "yahoo"
        if angel_one and prov != "yahoo" and (angel_one.configured() or prov == "angel"):
            try:
                base = yahoo_symbol(sym)[:-3]
                text = angel_one.Angel(record=record).chart_json(base, interval)
                LAST_SOURCE[sym] = "angelone"
                return text
            except angel_one.AngelError as e:
                if prov == "angel":
                    raise BarsError("Angel One: %s" % e)
                LAST_SOURCE[sym] = "yahoo (Angel One failed: %s)" % str(e)[:80]
    if offline:
        LAST_SOURCE[sym] = "offline"
        names = [offline_name(sym, interval)]
        if interval == "5m":
            base = yahoo_symbol(sym)[:-3] if is_nse(sym) else sym
            names.append("%s-60d5m.json" % base.upper())
        for n in names:
            for cand in (n, n + ".gz"):
                p = os.path.join(offline, cand)
                if os.path.exists(p):
                    with (gzip.open(p, "rt", encoding="utf-8") if cand.endswith(".gz") else
                          open(p, encoding="utf-8")) as fh:
                        return fh.read()
        raise BarsError("no offline chart %s in %s" % (names[0], offline))
    last = "no answer"
    for attempt in (0, 1):
        for host in HOSTS:
            time.sleep(pause)
            try:
                text = _get(chart_url(sym, interval, rng, host))
                LAST_SOURCE.setdefault(sym, "yahoo")
                if record:
                    os.makedirs(record, exist_ok=True)
                    with open(os.path.join(record, offline_name(sym, interval)), "w", encoding="utf-8") as fh:
                        fh.write(text)
                return text
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 404:
                    raise BarsError("Yahoo has no chart for %s" % yahoo_symbol(sym))
                if e.code not in (429, 500, 502, 503, 504):
                    raise BarsError("Yahoo answered %s for %s" % (last, yahoo_symbol(sym)))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))[:120]
        if attempt == 0:
            time.sleep(2.0)
    raise BarsError("could not get %s %s bars from Yahoo (%s)" % (yahoo_symbol(sym), interval, last))


def parse(text, now_utc=None, nse_session=None):
    """Chart JSON -> bars oldest first, in India time. For NSE symbols (or nse_session=True) only regular
    session bars that are complete at now_utc; other symbols (commodities, indices abroad) keep every bar."""
    try:
        doc = json.loads(text) if isinstance(text, str) else text
        res = (doc.get("chart") or {}).get("result") or []
    except (ValueError, AttributeError):
        raise BarsError("the chart is not Yahoo chart JSON")
    if not res:
        err = ((doc.get("chart") or {}).get("error") or {}).get("description") if isinstance(doc, dict) else None
        raise BarsError("the chart is empty%s" % (": " + err if err else ""))
    r = res[0]
    meta = r.get("meta") or {}
    gran = meta.get("dataGranularity") or "5m"
    step = STEP.get(gran, 5)
    sym = meta.get("symbol") or ""
    nse = is_nse(sym) if nse_session is None else nse_session
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    now = now_utc or _utc(time.time())
    out = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        o, h, lo, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, lo, c):
            continue
        start = _utc(ts)
        local = start + IST
        if step < 1440:
            mins = local.hour * 60 + local.minute
            if nse and (local.second or (mins - OPEN_MIN) % step):
                continue                                   # the live tick, off the 9:15-based grid (60m: 9:15, 10:15...)
            if start + _dt.timedelta(minutes=step) > now:
                continue                                   # not complete yet
            if nse and not (OPEN_MIN <= mins < CLOSE_MIN):
                continue
        out[int(ts)] = {"date": local.date().isoformat(), "time": local.strftime("%H:%M") if step < 1440 else "",
                        "ts": int(ts), "o": float(o), "h": float(h), "l": float(lo), "c": float(c), "v": float(v or 0)}
    return [out[t] for t in sorted(out)]


def resample(bars, minutes):
    """Bars -> `minutes` bars aligned to 9:15 within each session (15 -> 9:15, 9:30, ...)."""
    out, cur, key = [], None, None
    for b in bars:
        hh, mm = map(int, b["time"].split(":"))
        k = (b["date"], (hh * 60 + mm - OPEN_MIN) // minutes)
        if k != key:
            if cur:
                out.append(cur)
            start = OPEN_MIN + k[1] * minutes
            cur = dict(b, time="%02d:%02d" % (start // 60, start % 60))
            key = k
        else:
            cur["h"], cur["l"] = max(cur["h"], b["h"]), min(cur["l"], b["l"])
            cur["c"], cur["v"] = b["c"], cur["v"] + b["v"]
    if cur:
        out.append(cur)
    return out


def from_series(series):
    """nse_data's daily series {"t", "o", "h", "l", "c", "v"} -> bars."""
    out = []
    for i, ts in enumerate(series.get("t") or []):
        vals = [series.get(k)[i] if series.get(k) else None for k in ("o", "h", "l", "c", "v")]
        if None in vals[:4]:
            continue
        d = (_utc(ts) + IST).date().isoformat()
        out.append({"date": d, "time": "", "ts": int(ts), "o": float(vals[0]), "h": float(vals[1]),
                    "l": float(vals[2]), "c": float(vals[3]), "v": float(vals[4] or 0)})
    return out


def nse_daily(symbol, months=6, offline=None):
    """Daily bars from NSE (the bhavcopy MCP server); offline=DIR reads DIR/<SYM>_1d.json (Yahoo JSON or
    a list of bars)."""
    if offline:
        for cand in (offline_name(symbol, "1d"), offline_name(symbol, "1d") + ".gz"):
            p = os.path.join(offline, cand)
            if os.path.exists(p):
                with (gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")) as fh:
                    text = fh.read()
                doc = json.loads(text)
                return doc if isinstance(doc, list) else parse(doc)
        raise BarsError("no offline daily chart %s in %s" % (offline_name(symbol, "1d"), offline))
    import nse_data
    data, errors = nse_data.fetch_daily([symbol], months=months)
    if symbol.upper() not in data and symbol not in data:
        raise BarsError("NSE daily bars for %s: %s" % (symbol, errors.get(symbol) or errors or "none"))
    return from_series(data.get(symbol.upper()) or data.get(symbol))


# ---------------------------------------------------------------- measures

def true_ranges(bars):
    out = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["o"]
        out.append(max(b["h"], pc) - min(b["l"], pc))
    return out


def atr(bars, n=14, upto=None):
    """Average true range of the n bars ending at `upto` (inclusive, default the last). None if too few."""
    end = len(bars) if upto is None else upto + 1
    if end < 2:
        return None
    tr = true_ranges(bars[:end])[1:]
    tr = tr[-n:]
    return sum(tr) / len(tr) if tr else None


def atr_series(bars, n=14):
    """ATR at every bar (the average of the last n true ranges), None while fewer than 2 bars."""
    tr = true_ranges(bars)
    out, run = [], 0.0
    for i in range(len(bars)):
        if i == 0:
            out.append(None)
            continue
        run += tr[i]
        if i > n:
            run -= tr[i - n]
        out.append(run / min(i, n))
    return out


def ema(values, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def vwap(bars, start=0, end=None):
    """Volume-weighted typical price over bars[start:end+1]. Falls back to the plain average if no volume."""
    rows = bars[start:(len(bars) if end is None else end + 1)]
    vol = sum(b["v"] for b in rows)
    if not rows:
        return None
    if vol <= 0:
        return sum((b["h"] + b["l"] + b["c"]) / 3 for b in rows) / len(rows)
    return sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in rows) / vol


def session_vwap(bars, i):
    """VWAP of bar i's session up to and including bar i."""
    d = bars[i]["date"]
    j = i
    while j > 0 and bars[j - 1]["date"] == d:
        j -= 1
    return vwap(bars, j, i)


def anchored_vwap(bars, anchor_date, i=None):
    """VWAP from the first bar on or after anchor_date ('YYYY-MM-DD') to bar i (default the last)."""
    end = len(bars) - 1 if i is None else i
    for j, b in enumerate(bars[:end + 1]):
        if b["date"] >= anchor_date:
            return vwap(bars, j, end)
    return None


def vol_avg(bars, i, n=20):
    rows = bars[max(0, i - n):i]
    return sum(b["v"] for b in rows) / len(rows) if rows else None


def slot_rvol(bars, i, sessions=20, min_sessions=5):
    """Bar i's volume over the MEDIAN volume of the same clock slot (same start time) in the previous
    `sessions` sessions. Opening bars trade 3-10x the afternoon's, so comparing a 09:20 bar with the 20 bars
    before it (yesterday's afternoon) calls every open "heavy"; the same slot on other days does not.
    None for daily bars or with fewer than min_sessions earlier sessions."""
    b = bars[i]
    if not b.get("time"):
        return None
    vols = []
    for x in reversed(bars[:i]):
        if x["date"] < b["date"] and x.get("time") == b["time"]:
            vols.append(x["v"])
            if len(vols) >= sessions:
                break
    if len(vols) < min_sessions:
        return None
    vols.sort()
    med = vols[len(vols) // 2]
    return round(b["v"] / med, 2) if med > 0 else None


def rvol(bars, i, n=20, sessions=20):
    """(relative volume, how): the same-slot median when there is enough history (intraday), else the plain
    average of the previous n bars (daily bars, short history)."""
    r = slot_rvol(bars, i, sessions)
    if r is not None:
        return r, "same slot, last %d sessions" % sessions
    return vol_ratio(bars, i, n), "previous %d bars" % n


def vol_ratio(bars, i, n=20):
    """Bar i's volume over the average of the n bars before it (None when there is no history)."""
    a = vol_avg(bars, i, n)
    return round(bars[i]["v"] / a, 2) if a else None
