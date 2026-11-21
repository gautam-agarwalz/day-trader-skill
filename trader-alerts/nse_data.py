"""NSE market data for the day-trader skills: NSE's official MCP servers, plus a backup hook.

This file is shared: the same copy lives in trader-scanner/ and trader-alerts/ (a repo test
keeps them identical), because each skill folder must work on its own once copied.

Servers (Streamable HTTP, public, no login), called with the official `mcp` Python SDK:

  LIVE_URL  https://mcp.nseindia.in/cmmkt/mcp        CM Market Live: live quotes, the equity
                                                     list, indices (refreshed every minute)
  EOD_URL   https://mcp.nseindia.in/bhavcopy/cm/mcp  CM Bhavcopy: daily OHLCV for the last
                                                     5 years, market breadth

These are plain MCP tool calls (JSON in, JSON out). No language model is involved anywhere.

Symbols are NSE's own: RELIANCE, TCS, M&M. A Yahoo-style ".NS" suffix is accepted and dropped.

When an NSE call times out, fails, or comes back empty, the affected symbols go to
fetch_backup_data() automatically. Today that reads the local CSV cache that every successful
NSE call refreshes (trader/cache/ by default); it is the one place to add a live backup
provider later.
"""

import asyncio
import csv
import datetime
import json
import os

LIVE_URL = "https://mcp.nseindia.in/cmmkt/mcp"
EOD_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"
TIMEOUT = 20.0          # seconds for one tool call
SESSION_TIMEOUT = 900.0  # seconds for a whole batch on one connection
CONCURRENCY = 6          # tool calls in flight on one connection: be polite to NSE
QUOTE_TIMEOUT = 45.0     # seconds for one live quote: NSE answers in ~10 s and queues calls one by one
QUOTE_CONCURRENCY = 2
EQUITY_PAGE = 500        # cm_get_equity_stocks returns at most 500 rows a call
EQUITY_TIMEOUT = 180.0   # seconds for one equity-list page: NSE takes 30 to 60 s to build one
EQUITY_CONCURRENCY = 9
CACHE_DIR = os.path.join("trader", "cache")
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)
QUOTE_FIELDS = ("symbol", "price", "open", "high", "low", "prev_close", "volume", "time", "fetched", "source")
DAILY_FIELDS = ("date", "open", "high", "low", "close", "volume")


class NSEError(Exception):
    pass


def nse_symbol(sym):
    """RELIANCE.NS or reliance -> RELIANCE (NSE's own spelling)."""
    s = str(sym or "").strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def _iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the MCP client

def _payload(result):
    """A CallToolResult -> the JSON it carries. Raises NSEError for an error or an empty answer."""
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        texts = [getattr(c, "text", "") for c in (result.content or [])]
        raise NSEError("NSE error: %s" % (" ".join(t for t in texts if t)[:200] or "unknown"))
    text = "".join(getattr(c, "text", "") or "" for c in (result.content or []))
    if not text.strip():
        raise NSEError("empty response")
    try:
        doc = json.loads(text)
    except ValueError:
        raise NSEError("not JSON: %s" % text[:120])
    if isinstance(doc, dict) and doc.get("error"):
        raise NSEError(str(doc["error"]))
    if doc in (None, {}, []):
        raise NSEError("empty response")
    return doc


async def _call_many(url, calls, timeout, concurrency):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    out = [None] * len(calls)
    async with streamable_http_client(url, terminate_on_close=False) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await asyncio.wait_for(session.initialize(), timeout)
            sem = asyncio.Semaphore(max(1, concurrency))

            async def one(i, name, args):
                async with sem:
                    try:
                        res = await asyncio.wait_for(session.call_tool(name, args), timeout)
                        out[i] = (True, _payload(res))
                    except asyncio.TimeoutError:
                        out[i] = (False, "timed out after %ds" % timeout)
                    except NSEError as e:
                        out[i] = (False, str(e))
                    except Exception as e:  # one bad call must not sink the batch
                        out[i] = (False, "failed: %s" % (str(e) or type(e).__name__))
            await asyncio.gather(*[one(i, n, a) for i, (n, a) in enumerate(calls)])
    return out


def call_tools(url, calls, timeout=TIMEOUT, concurrency=CONCURRENCY, session_timeout=SESSION_TIMEOUT):
    """[(tool, args), ...] -> [(ok, payload_or_error), ...] in the same order, over one connection.
    A connection that cannot be made (or the mcp SDK missing) fails every call, with the reason."""
    if not calls:
        return []
    try:
        import mcp  # noqa: F401  (the SDK: pip install -r requirements.txt)
    except ImportError:
        return [(False, "the mcp SDK is not installed (pip install -r requirements.txt)")] * len(calls)
    try:
        res = asyncio.run(asyncio.wait_for(_call_many(url, calls, timeout, concurrency), session_timeout))
    except asyncio.TimeoutError:
        return [(False, "timed out: no answer from %s" % url)] * len(calls)
    except BaseException as e:  # connection refused, DNS, proxy, TLS... (anyio wraps them in groups)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        inner = e
        while getattr(inner, "exceptions", None):
            inner = inner.exceptions[0]
        return [(False, "could not reach NSE (%s): %s" % (url, str(inner) or type(inner).__name__))] * len(calls)
    return [r if r is not None else (False, "no answer") for r in res]


# ---------------------------------------------------------------- parsers (pure)

def ist_text_to_utc(text):
    """NSE's "2026-11-20 16:00:28" (India time) -> "2026-11-20T10:30:28Z"."""
    try:
        local = datetime.datetime.strptime(str(text).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return _iso(local - IST)


def parse_live_quote(doc, sym=""):
    """cm_get_stock_quote's answer -> one quote. Raises NSEError when it holds no price."""
    st = (doc or {}).get("stock") if isinstance(doc, dict) else None
    if not isinstance(st, dict):
        raise NSEError("no quote for %s" % sym)
    price = _num(st.get("lastTradedPrice"))
    if not price:
        raise NSEError("no price for %s" % sym)
    return {"symbol": nse_symbol(st.get("symbol") or sym), "price": price,
            "open": _num(st.get("openPrice")) or None, "high": _num(st.get("highPrice")) or None,
            "low": _num(st.get("lowPrice")) or None, "prev_close": _num(st.get("preClosePrice")),
            "volume": int(_num(st.get("volume")) or 0), "time": ist_text_to_utc(st.get("latestTimestamp")),
            "series": st.get("series"), "currency": "INR", "source": "nse"}


def day_ts(date_text):
    """A trading date -> unix seconds of that day's 9:15 IST open (how the scanner dates a daily bar)."""
    d = datetime.datetime.strptime(str(date_text)[:10], "%Y-%m-%d")
    return int((d + datetime.timedelta(hours=9, minutes=15) - IST - EPOCH).total_seconds())


def rows_to_series(rows, sym=""):
    """[{date, open, high, low, close, volume}] (any order) -> the scanner's daily series."""
    by_day = {}
    for r in rows:
        if r.get("date") and _num(r.get("close")) is not None:
            by_day[str(r["date"])[:10]] = r
    days = sorted(by_day)
    if not days:
        raise NSEError("no daily bars for %s" % sym)
    pick = [by_day[d] for d in days]
    last = pick[-1]
    return {"t": [day_ts(d) for d in days], "o": [_num(r.get("open")) for r in pick],
            "h": [_num(r.get("high")) for r in pick], "l": [_num(r.get("low")) for r in pick],
            "c": [_num(r.get("close")) for r in pick], "v": [_num(r.get("volume")) for r in pick],
            "meta": {"symbol": nse_symbol(sym), "regularMarketPrice": _num(last.get("close")),
                     "regularMarketVolume": _num(last.get("volume")), "currency": "INR"}}


def history_rows(doc):
    """get_stock_history's answer -> [{date, open, high, low, close, volume}] (series EQ/BE etc. kept)."""
    data = (doc or {}).get("data") if isinstance(doc, dict) else None
    if not data:
        return []
    return [{"date": r.get("date"), "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
             "close": r.get("close"), "volume": r.get("volume")} for r in data if isinstance(r, dict)]


def parse_history(doc, sym=""):
    """get_stock_history's answer (one chunk) -> the scanner's daily series."""
    return rows_to_series(history_rows(doc), sym or (doc or {}).get("symbol", ""))


def is_history(doc):
    return isinstance(doc, dict) and isinstance(doc.get("data"), list) and "from_date" in doc


def is_equity_page(doc):
    return isinstance(doc, dict) and isinstance(doc.get("stocks"), list) and "segment" in doc


def equity_rows(doc, series=("EQ",)):
    """cm_get_equity_stocks' answer -> [{symbol, series, price, volume}] for the wanted series."""
    out = []
    for s in (doc or {}).get("stocks") or []:
        if not isinstance(s, dict) or not s.get("symbol"):
            continue
        if series and s.get("series") not in series:
            continue
        out.append({"symbol": nse_symbol(s["symbol"]), "series": s.get("series"),
                    "price": _num(s.get("lastTradedPrice")), "volume": int(_num(s.get("volume")) or 0)})
    return out


# ---------------------------------------------------------------- the CSV cache

def _cache(cache_dir):
    return cache_dir or CACHE_DIR


def save_quotes_to_cache(quotes, cache_dir=None, fetched=None):
    """Merge fresh NSE quotes into <cache>/quotes.csv (one row a symbol, the newest wins)."""
    if not quotes:
        return
    path = os.path.join(_cache(cache_dir), "quotes.csv")
    rows = {r["symbol"]: r for r in _read_csv(path)}
    stamp = fetched or _iso(_utc_now())
    for sym, q in quotes.items():
        rows[sym] = {k: q.get(k) for k in QUOTE_FIELDS}
        rows[sym].update(symbol=sym, fetched=stamp, source="nse")
    _write_csv(path, QUOTE_FIELDS, [rows[k] for k in sorted(rows)])


def save_daily_to_cache(sym, rows, cache_dir=None):
    """Merge NSE daily bars into <cache>/daily/<SYM>.csv (one row a date)."""
    if not rows:
        return
    path = os.path.join(_cache(cache_dir), "daily", "%s.csv" % _safe(sym))
    have = {r["date"]: r for r in _read_csv(path)}
    for r in rows:
        if r.get("date"):
            have[str(r["date"])[:10]] = {k: r.get(k) for k in DAILY_FIELDS}
            have[str(r["date"])[:10]]["date"] = str(r["date"])[:10]
    _write_csv(path, DAILY_FIELDS, [have[d] for d in sorted(have)])


def _safe(sym):
    return nse_symbol(sym).replace("/", "_")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path, fields, rows):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


# ---------------------------------------------------------------- the backup hook

def fetch_backup_data(symbols, kind="quotes", cache_dir=None, reasons=None):
    """The backup data source. Called automatically for every symbol the NSE MCP servers could not
    serve: a timeout, a failed call or connection, or an empty answer.

    symbols  NSE symbols (RELIANCE, TCS, ...)
    kind     "quotes" -> {SYM: quote dict, same keys as parse_live_quote}
             "daily"  -> {SYM: daily series, same shape as parse_history}
    reasons  {SYM: why NSE failed}, passed through for logging / a provider's own choices

    Returns (data, errors). Every result is marked with its "source" so the caller can say
    where a number came from; a cached quote keeps its original quote "time", so a stale
    price stays visibly stale (trader-risk only uses a price under 15 minutes old).
    """
    syms = [nse_symbol(s) for s in symbols]
    data, errors = {}, {}

    # TODO: Plug in Angel One SmartAPI or TradingView backup here.
    #   Try the live provider first for `syms`, put what it returns into `data` (same shapes as
    #   above, with "source": "angelone" / "tradingview"), and leave the rest to the cache below.
    #   e.g. data.update(_angel_one_quotes(syms) if kind == "quotes" else _angel_one_daily(syms))
    #   Keep credentials out of this file (environment variables or trader/config.json).

    remaining = [s for s in syms if s not in data]
    if kind == "quotes":
        cached = {r["symbol"]: r for r in _read_csv(os.path.join(_cache(cache_dir), "quotes.csv"))}
        for s in remaining:
            r = cached.get(s)
            if not r or _num(r.get("price")) is None:
                errors[s] = _why(s, reasons, "no cached quote")
                continue
            q = {k: r.get(k) or None for k in QUOTE_FIELDS}
            for k in ("price", "open", "high", "low", "prev_close"):
                q[k] = _num(q[k])
            q["volume"] = int(_num(q.get("volume")) or 0)
            q.update(symbol=s, time=r.get("time") or "", currency="INR", source="cache",
                     cached_at=r.get("fetched") or "")
            data[s] = q
    elif kind == "daily":
        for s in remaining:
            rows = _read_csv(os.path.join(_cache(cache_dir), "daily", "%s.csv" % _safe(s)))
            try:
                series = rows_to_series(rows, s)
            except NSEError:
                errors[s] = _why(s, reasons, "no cached daily bars")
                continue
            series["meta"]["source"] = "cache"
            data[s] = series
    else:
        raise NSEError("fetch_backup_data: kind is quotes or daily, not %r" % kind)
    return data, errors


def _why(sym, reasons, own):
    r = (reasons or {}).get(sym)
    return "%s; backup: %s" % (r, own) if r else own


# ---------------------------------------------------------------- what the tools call

def fetch_quotes(symbols, cache_dir=None, timeout=QUOTE_TIMEOUT, concurrency=QUOTE_CONCURRENCY):
    """Live quotes from NSE (cm_get_stock_quote, one call a symbol over one connection).
    -> (quotes {SYM: quote}, errors {SYM: why}). Failures go to fetch_backup_data()."""
    syms = _unique(symbols)
    res = call_tools(LIVE_URL, [("cm_get_stock_quote", {"symbol": s}) for s in syms], timeout=timeout,
                     concurrency=concurrency)
    quotes, failed = {}, {}
    for s, (ok, payload) in zip(syms, res):
        if not ok:
            failed[s] = payload
            continue
        try:
            quotes[s] = parse_live_quote(payload, s)
        except NSEError as e:
            failed[s] = str(e)
    save_quotes_to_cache(quotes, cache_dir)
    if failed:
        got, errors = fetch_backup_data(list(failed), "quotes", cache_dir, failed)
        quotes.update(got)
        return quotes, errors
    return quotes, {}


def fetch_daily(symbols, months=3, cache_dir=None, timeout=TIMEOUT, end_date="today"):
    """Daily OHLCV from NSE's bhavcopy server (get_stock_history, 3 months a call, chained for
    more: up to 60 months = the 5 years NSE keeps). -> (series {SYM: ...}, errors {SYM: why}).
    Failures go to fetch_backup_data()."""
    syms = _unique(symbols)
    months = max(1, min(int(months), 60))
    rows = {s: [] for s in syms}
    failed = {}
    todo = {s: end_date for s in syms}
    left = months
    while todo and left > 0:
        chunk = min(3, left)
        batch = sorted(todo)
        res = call_tools(EOD_URL, [("get_stock_history", {"symbol": s, "months": chunk, "endDate": todo[s]})
                                   for s in batch], timeout=timeout)
        nxt = {}
        for s, (ok, payload) in zip(batch, res):
            got = history_rows(payload) if ok else []
            if not ok or not got:
                if not rows[s]:
                    failed[s] = payload if not ok else "empty response"
                continue
            rows[s].extend(got)
            if payload.get("next_end_date"):
                nxt[s] = payload["next_end_date"]
        todo, left = nxt, left - chunk
    data = {}
    for s in syms:
        if s in failed:
            continue
        try:
            data[s] = rows_to_series(rows[s], s)
            data[s]["meta"]["source"] = "nse"
            save_daily_to_cache(s, rows[s], cache_dir)
        except NSEError as e:
            failed[s] = str(e)
    if failed:
        got, errors = fetch_backup_data(list(failed), "daily", cache_dir, failed)
        data.update(got)
        return data, errors
    return data, {}


def fetch_equity_list(series=("EQ",), timeout=EQUITY_TIMEOUT, concurrency=EQUITY_CONCURRENCY):
    """Every NSE capital-market equity symbol in `series` (EQ by default) from the live server,
    paged by symbol prefix (a page holds at most 500). -> [{symbol, series, price, volume}].
    Raises NSEError if NSE cannot be reached: a universe is never built from a backup."""
    prefixes = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]
    rows = {}
    while prefixes:
        res = call_tools(LIVE_URL, [("cm_get_equity_stocks", {"limit": EQUITY_PAGE, "symbolFilter": p})
                                    for p in prefixes], timeout=timeout, concurrency=concurrency)
        deeper = []
        for p, (ok, payload) in zip(prefixes, res):
            if not ok:
                if "Symbol" in payload and "not found" in payload:
                    continue
                raise NSEError("NSE equity list (prefix %s): %s" % (p, payload))
            stocks = payload.get("stocks") or []
            if len(stocks) >= EQUITY_PAGE:
                deeper.extend(p + c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-")
                continue
            for r in equity_rows(payload, series):
                rows[r["symbol"]] = r
        prefixes = deeper
    if not rows:
        raise NSEError("NSE returned no equity symbols")
    return [rows[k] for k in sorted(rows)]


def _unique(symbols):
    out = []
    for s in symbols:
        s = nse_symbol(s)
        if s and s not in out:
            out.append(s)
    return out
