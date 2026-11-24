#!/usr/bin/env python3
"""Global Macro News & Equity-First Bidirectional NSE Trade Matrix (trader-scanner): global shocks
(gold, war, crude, US tariffs / China+1, the Fed, metals) and big company events, checked through three
gates, then mapped both ways onto NSE stocks. Keyword rules and arithmetic only, no language model.

    python3 global_macro_engine.py                    # headlines -> gates -> trader/macro/latest.json
    python3 global_macro_engine.py --themes gold,crude
    python3 global_macro_engine.py --hours 6 --json   # a wider headline window
    python3 global_macro_engine.py --offline DIR --now "2026-11-24 10:40" --dry-run

Headlines: the GDELT 2.0 DOC API (the gdeltdoc package; the plain API URL when it is not installed or
--offline is set), plus the RSS feeds in the map (Economic Times, Moneycontrol, LiveMint, RBI, the Fed,
PIB) and a Google News search per theme. Company events come from trader/orders/latest.json
(govt_orders_tracker.py) and trader/catalysts/latest.json (news.py).

The theme -> stock matrix is macro_map.json in this folder (copy it to trader/macro_map.json to edit):
each theme has keywords, words that say which way it went, price proxies with thresholds (Yahoo:
GC=F gold, CL=F / BZ=F crude, HG=F copper, ^VIX, ^TNX, DX-Y.NYB) and, for each direction, the stocks to
go long, to go short and to be careful with. The defaults are examples, not findings.

The three gates (every event shows each gate's evidence):
  1. Source   a tier-1 source (an exchange, a government, a central bank: the map's tier1_domains, or an
              NSE / BSE filing) OR at least 2 different outlets on the same theme and direction within
              15 minutes of each other
  2. Size     a proxy moved past its threshold the expected way (gold >= 1.5 %, crude >= 3 % ...); for a
              company event the order is >= 5 % of market cap, or profit / revenue >= 20 % on last year
  3. Market   5 to 15 minutes after the news, a 5-minute bar of the stock trades >= 2.0x its 20-bar
              average volume and the price has moved the expected way since the news. Before 5 minutes:
              pending; past 15 minutes with no such bar: dropped. News outside market hours starts its
              clock at the next 9:15 open. (Volume, not proof of who traded.)
Lists: [ACTIONABLE LONG] / [ACTIONABLE SHORT] (all three gates passed), [CAUTION / BLOCK ENTRY] (the
theme's caution list, the other side of a passed event, and stocks whose gate 3 went the wrong way).
Routing is equity first: [EQUITY INTRADAY LONG], [EQUITY SWING LONG: 2-3+ DAYS] (a persistent theme or
a bullish daily trend), [EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM] for every cash short, and
[SECONDARY F&O SWING SHORT OPTION] only for an F&O stock, out of the ban period, in a multi-day
downtrend. Next: order_block_scanner.py for the entry, trader-timemachine/brief.py for the odds.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_bars as ib  # noqa: E402
import india_http as ih  # noqa: E402
import india_market as im  # noqa: E402
import order_block_scanner as obs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "macro_map.json")
USER_MAP = os.path.join("trader", "macro_map.json")
OUT_DIR = os.path.join("trader", "macro")
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc?query=%s&mode=%s&format=json&maxrecords=75&timespan=%s&sort=datedesc"
GNEWS = "https://news.google.com/rss/search?q=%s+when:1d&hl=en-IN&gl=IN&ceid=IN:en"


def load_map(path=None):
    for p in (path, USER_MAP, MAP):
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh), p
    raise SystemExit("global_macro_engine.py: no macro_map.json")


# ---------------------------------------------------------------- headlines

def domain_of(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def gdelt_time(s):
    """GDELT seendate '20261124T051500Z' (UTC) -> 'YYYY-MM-DD HH:MM' India time."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})", str(s or ""))
    if not m:
        return ""
    d = _dt.datetime(*map(int, m.groups())) + ib.IST
    return d.strftime("%Y-%m-%d %H:%M")


def gdelt_articles(client, query, hours, notes, use_package=True, keywords=None):
    """[{title, link, domain, time, source}] from GDELT's DOC API. The gdeltdoc package takes a keyword list
    (it ORs them, quoting phrases); the plain API takes the boolean query."""
    span = "%dh" % max(1, hours)
    if use_package and not client.offline:
        try:
            from gdeltdoc import GdeltDoc, Filters
            kws = [k.strip() for k in (keywords or []) if len(k.strip()) >= 3][:12] or query
            f = Filters(keyword=kws, timespan=span, num_records=75)
            df = GdeltDoc().article_search(f)
            rows = df.to_dict("records") if df is not None else []
            return [{"title": r.get("title") or "", "link": r.get("url") or "", "domain": r.get("domain") or "",
                     "time": gdelt_time(r.get("seendate")), "source": "GDELT"} for r in rows]
        except ImportError:
            notes.append("gdeltdoc is not installed (pip install -r requirements.txt); using the plain GDELT API")
        except Exception as e:  # the package raises on an empty or rate-limited answer
            notes.append("gdeltdoc: %s; trying the plain API" % str(e)[:120])
    url = GDELT % (urllib.parse.quote(query), "artlist", span)
    doc = client.get_json(url)
    return [{"title": a.get("title") or "", "link": a.get("url") or "", "domain": a.get("domain") or "",
             "time": gdelt_time(a.get("seendate")), "source": "GDELT"} for a in (doc or {}).get("articles") or []]


def gdelt_tone(client, query, hours):
    """The average tone of the last hour against the window before it: a sentiment spike, or None."""
    url = GDELT % (urllib.parse.quote(query), "timelinetone", "%dh" % max(6, hours * 2))
    doc = client.get_json(url)
    data = [p.get("value") for s in (doc or {}).get("timeline") or [] for p in s.get("data") or []
            if isinstance(p.get("value"), (int, float))]
    if len(data) < 6:
        return None
    last, base = data[-1], data[:-1]
    mu = sum(base) / len(base)
    sd = (sum((x - mu) ** 2 for x in base) / len(base)) ** 0.5
    return {"last": round(last, 2), "mean": round(mu, 2), "z": round((last - mu) / sd, 2) if sd else None}


DEAD = set()          # hosts that refused this pass: one note each, no more tries


def rss_articles(client, url, notes):
    host = domain_of(url)
    if host in DEAD:
        return []
    try:
        out = []
        for e in ih.parse_rss(client.get_bytes(url)):
            title = e["title"]
            m = re.match(r"^(.*\S)\s+-\s+([^-]{2,60})$", title)
            outlet = e.get("outlet") or (m.group(2) if m and "news.google" in url else "")
            if m and "news.google" in url:
                title = m.group(1)
            out.append({"title": title, "summary": re.sub(r"<[^>]+>", " ", e.get("summary") or ""), "link": e["link"],
                        "domain": domain_of(e.get("outlet_url")) or (outlet.lower() if outlet else domain_of(e["link"])),
                        "time": e["time"], "source": "RSS %s" % domain_of(url)})
        return out
    except ih.FeedError as e:
        if "could not reach" in str(e) or "refuses this network" in str(e):
            DEAD.add(host)
            notes.append("feed %s is unreachable from here; its feeds were skipped (%s)" % (host, str(e)[:80]))
        else:
            notes.append("feed %s: %s" % (host, str(e)[:120]))
        return []


def _has(text, words):
    t = " %s " % text.lower()
    return any(w.lower() in t for w in words)


def _count(text, words):
    t = " %s " % text.lower()
    return sum(1 for w in words if w.lower() in t)


def direction_of(text, theme):
    """'up' / 'down' by which side's words appear more often; a tie is no direction."""
    up, down = _count(text, theme.get("up_words") or []), _count(text, theme.get("down_words") or [])
    if up > down:
        return "up"
    if down > up:
        return "down"
    return None


def theme_hits(articles, themes):
    """{(theme, direction): [article]} for headlines naming a theme with a clear direction."""
    out = {}
    for a in articles:
        text = "%s %s" % (a["title"], a.get("summary") or "")
        for name, th in themes.items():
            if not _has(text, th["keywords"]):
                continue
            d = direction_of(text, th)
            if d:
                out.setdefault((name, d), []).append(dict(a, theme=name, direction=d))
    return out


# ---------------------------------------------------------------- the gates

def gate1(arts, tier1, window=15):
    """Tier-1 source, or >= 2 different outlets within `window` minutes. -> (ok, why, the time it passed)."""
    arts = sorted((a for a in arts if a["time"]), key=lambda a: a["time"])
    for a in arts:
        if any(a["domain"] == d or a["domain"].endswith("." + d) for d in tier1) or a.get("tier1"):
            return True, "tier-1 source %s: %s" % (a["domain"], a["title"][:90]), a["time"]
    for i, a in enumerate(arts):
        t0 = _dt.datetime.strptime(a["time"], "%Y-%m-%d %H:%M")
        seen = {a["domain"]}
        for b in arts[i + 1:]:
            if _dt.datetime.strptime(b["time"], "%Y-%m-%d %H:%M") - t0 > _dt.timedelta(minutes=window):
                break
            if b["domain"] and b["domain"] not in seen:
                return True, "%s and %s within %d minutes" % (a["domain"], b["domain"], window), b["time"]
            seen.add(b["domain"])
    outlets = sorted({a["domain"] for a in arts if a["domain"]})
    return False, "%d headline(s) from %s: no tier-1 source and no second outlet within %d minutes" % (
        len(arts), ", ".join(outlets[:4]) or "-", window), None


def proxy_move(bars):
    """(last close / previous session's close - 1) in %, from a proxy's bars."""
    if len(bars) < 2:
        return None
    last = bars[-1]
    prev = [b for b in bars if b["date"] < last["date"]]
    if not prev:
        return None
    return round((last["c"] / prev[-1]["c"] - 1) * 100, 2)


def gate2_macro(theme, direction, moves):
    """A proxy past its threshold the expected way. moves = {proxy: % move or None}."""
    want = 1 if direction == "up" else -1
    seen = []
    for p in theme.get("proxies") or []:
        mv = moves.get(p["symbol"])
        if mv is None:
            seen.append("%s n/a" % p["symbol"])
            continue
        seen.append("%s %+.2f%%" % (p["symbol"], mv))
        if mv * p.get("sign", 1) * want >= p["threshold_pct"]:
            return True, "%s moved %+.2f%% (threshold %.1f%%)" % (p["symbol"], mv, p["threshold_pct"])
    return False, "no proxy past its threshold (%s)" % ", ".join(seen) if seen else "no proxy set"


def effective_start(t_text):
    """News outside market hours starts its gate-3 clock at the next 9:15 open."""
    t = _dt.datetime.strptime(t_text, "%Y-%m-%d %H:%M")
    d = t.date()
    if im.trading_day(d) and "09:15" <= t.strftime("%H:%M") < "15:30":
        return t
    if im.trading_day(d) and t.strftime("%H:%M") < "09:15":
        return _dt.datetime.combine(d, _dt.time(9, 15))
    d += _dt.timedelta(days=1)
    while not im.trading_day(d):
        d += _dt.timedelta(days=1)
    return _dt.datetime.combine(d, _dt.time(9, 15))


def gate3(bars, start, now, side, cfg):
    """{status: pending | confirmed | dropped | no data, ...} on 5-minute bars (India time)."""
    lo = start + _dt.timedelta(minutes=cfg.get("min_wait_minutes", 5))
    hi = start + _dt.timedelta(minutes=cfg.get("max_wait_minutes", 15))
    if now < lo:
        return {"status": "pending", "why": "wait until %s (5 minutes after %s)" % (lo.strftime("%H:%M"),
                                                                                  start.strftime("%H:%M"))}
    if not bars:
        return {"status": "no data", "why": "no 5-minute bars"}

    def end_of(b):
        return _dt.datetime.strptime("%s %s" % (b["date"], b["time"]), "%Y-%m-%d %H:%M") + _dt.timedelta(minutes=5)
    before = [b for b in bars if end_of(b) <= start]
    ref = before[-1]["c"] if before else None
    if ref is None:
        first = [b for b in bars if end_of(b) > start]
        ref = first[0]["o"] if first else None
    win = [(i, b) for i, b in enumerate(bars) if lo <= end_of(b) <= min(hi, now)]
    want = 1 if side == "long" else -1
    best = None
    for i, b in win:
        vr = ib.vol_ratio(bars, i, cfg.get("bars", 20))
        mv = (b["c"] / ref - 1) * 100 if ref else None
        if vr is not None and vr >= cfg.get("volume_ratio", 2.0) and mv is not None and mv * want > 0:
            return {"status": "confirmed", "bar": "%s %s" % (b["date"], end_of(b).strftime("%H:%M")),
                    "volume_ratio": vr, "move_pct": round(mv, 2), "why": "%.1fx volume, %+.2f%% since the news"
                                                                        % (vr, mv)}
        if vr is not None and (best is None or vr > best[0]):
            best = (vr, mv, b)
    if now <= hi:
        return {"status": "pending", "why": "no bar with %.1fx volume the right way yet (best %s); window ends %s"
                % (cfg.get("volume_ratio", 2.0), "%.1fx" % best[0] if best else "-", hi.strftime("%H:%M"))}
    wrong = best is not None and best[1] is not None and best[1] * want < 0 and best[0] >= cfg.get("volume_ratio", 2.0)
    return {"status": "wrong way" if wrong else "dropped",
            "why": ("heavy volume against the expected direction (%.1fx, %+.2f%%)" % (best[0], best[1]) if wrong else
                    "no 5-minute bar with %.1fx volume the expected way within 15 minutes" % cfg.get("volume_ratio", 2.0))}


# ---------------------------------------------------------------- company events

def company_events(base, cfg):
    """Orders >= 5 % of market cap and results >= 20 % on last year, from the other tools' outputs."""
    out = []
    ce = cfg.get("company_events") or {}
    for sub in ("orders", "catalysts"):
        doc = ih.load_json(os.path.join(base, sub, "latest.json")) or {}
        for it in doc.get("items") or []:
            sym, n = (it.get("symbol") or "").upper(), it.get("numbers") or {}
            if not sym or not it.get("time"):
                continue
            exch = (it.get("source") or "").startswith(("NSE", "BSE"))
            kind = it.get("kind") or ("results" if "results" in (it.get("tags") or []) else "")
            size = None
            if kind in ("award", "cancel", "delay") and n.get("impact_pct") is not None:
                if n["impact_pct"] >= ce.get("order_impact_pct", 5.0):
                    size = "order %.1f%% of market cap" % n["impact_pct"]
            yoy = [v for k, v in n.items() if k in ("profit_yoy", "revenue_yoy") and isinstance(v, (int, float))]
            if kind == "results" and yoy and max(abs(v) for v in yoy) >= ce.get("results_yoy_pct", 20.0):
                size = "results %s on last year" % ", ".join("%+.0f%%" % v for v in yoy)
            if not size:
                continue
            side = "short" if kind in ("cancel", "delay") or (kind == "results" and min(yoy) < 0 and max(yoy) <= 0) \
                else "long"
            out.append({"symbol": sym, "side": side, "time": it["time"][:16], "title": it.get("title") or "",
                        "link": it.get("link") or "", "tier1": exch, "size": size, "domain": domain_of(it.get("link")),
                        "kind": kind})
    return out


# ---------------------------------------------------------------- run

def run(args, notes):
    DEAD.clear()
    mp, map_path = load_map(args.map)
    themes = {k: v for k, v in mp["themes"].items() if not args.themes or k in args.themes.split(",")}
    client = ih.Client(offline=args.offline)
    now = ih.now_ist().replace(second=0, microsecond=0)
    now_utc = now - ib.IST
    arts = []
    if not args.no_gdelt:
        for name, th in themes.items():
            try:
                got = gdelt_articles(client, th.get("gdelt") or " OR ".join(th["keywords"]), args.hours, notes,
                                     use_package=not args.offline, keywords=th["keywords"])
                arts += got
            except ih.FeedError as e:
                notes.append("GDELT %s: %s" % (name, str(e)[:140]))
    for url in (mp.get("feeds") or []):
        arts += rss_articles(client, url, notes)
    for name, th in themes.items():
        q = urllib.parse.quote_plus(" OR ".join('"%s"' % k if " " in k else k for k in th["keywords"][:6]))
        arts += rss_articles(client, GNEWS % q, notes)
    since = (now - _dt.timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M")
    arts = [a for a in arts if a["time"] and since <= a["time"] <= now.strftime("%Y-%m-%d %H:%M")]
    hits = theme_hits(arts, themes)
    moves, proxies = {}, sorted({p["symbol"] for th in themes.values() for p in th.get("proxies") or []})
    for p in proxies:
        try:
            moves[p] = proxy_move(ib.parse(ib.fetch_text(p, "5m", rng="5d", offline=args.offline), now_utc))
        except ib.BarsError as e:
            moves[p] = None
            notes.append("proxy %s: %s" % (p, e))
    tier1 = mp.get("tier1_domains") or []
    events = []
    for (name, d), group in sorted(hits.items()):
        th = themes[name]
        g1, why1, t1 = gate1(group, tier1, mp.get("window_minutes", 15))
        g2, why2 = gate2_macro(th, d, moves)
        tone = None
        if not args.no_gdelt and not args.offline:
            try:
                tone = gdelt_tone(client, th.get("gdelt") or name, args.hours)
            except ih.FeedError:
                tone = None
        events.append({"kind": "macro", "theme": name, "label": th.get("label", name), "direction": d,
                       "headlines": len(group), "first": group and min(a["time"] for a in group),
                       "gate1": {"ok": g1, "why": why1, "time": t1}, "gate2": {"ok": g2, "why": why2},
                       "tone": tone, "persistent": th.get("persistent", False), "matrix": th[d],
                       "examples": [{"title": a["title"][:140], "link": a["link"], "domain": a["domain"],
                                     "time": a["time"]} for a in sorted(group, key=lambda a: a["time"])[:5]]})
    for ce in company_events(args.base, mp):
        ok1, why1, t1 = gate1([dict(ce, title=ce["title"])], tier1 + ["nseindia.com", "bseindia.com"])
        if ce["tier1"]:
            ok1, why1, t1 = True, "exchange filing (tier 1)", ce["time"]
        events.append({"kind": "company", "theme": ce["kind"], "label": "%s %s" % (ce["symbol"], ce["kind"]),
                       "direction": "up" if ce["side"] == "long" else "down", "headlines": 1, "first": ce["time"],
                       "gate1": {"ok": ok1, "why": why1, "time": t1}, "gate2": {"ok": True, "why": ce["size"]},
                       "persistent": True,
                       "matrix": {"long": [ce["symbol"]] if ce["side"] == "long" else [],
                                  "short": [ce["symbol"]] if ce["side"] == "short" else [], "caution": []},
                       "examples": [{"title": ce["title"][:140], "link": ce["link"], "domain": ce["domain"],
                                     "time": ce["time"]}]})
    longs, shorts, caution = [], [], []
    fno = {}
    try:
        fno = im.fno_lots(client) if not args.no_fno else {}
        ban = set((im.fno_ban(client) or {}).get("symbols") or []) if not args.no_fno else set()
    except ih.FeedError as e:
        ban = set()
        notes.append("F&O list: %s" % str(e)[:120])
    bars_cache = {}

    def five(sym):
        if sym not in bars_cache:
            try:
                bars_cache[sym] = ib.parse(ib.fetch_text(sym, "5m", offline=args.offline), now_utc)
            except ib.BarsError as e:
                notes.append("%s 5m: %s" % (sym, e))
                bars_cache[sym] = []
        return bars_cache[sym]

    def trend(sym):
        try:
            daily = [b for b in ib.nse_daily(sym, 3, offline=args.offline) if b["date"] < now.strftime("%Y-%m-%d")]
        except Exception:
            daily = obs.daily_from_intraday([b for b in five(sym) if b["date"] < now.strftime("%Y-%m-%d")])
        return obs.daily_trend(daily)

    g3cfg = mp.get("gate3") or {}
    for ev in events:
        passed = ev["gate1"]["ok"] and ev["gate2"]["ok"]
        ev["passed_gates_1_2"] = passed
        for c in ev["matrix"].get("caution") or []:
            caution.append({"symbol": c, "why": "%s %s: on the theme's caution list" % (ev["label"], ev["direction"]),
                            "event_time": ev["first"], "theme": ev["theme"], "gates_1_2": passed})
        if not passed:
            continue
        start = effective_start(ev["gate1"]["time"] or ev["first"])
        for side, rows in (("long", ev["matrix"].get("long") or []), ("short", ev["matrix"].get("short") or [])):
            for sym in rows:
                g3 = gate3(five(sym), start, now, side, g3cfg)
                tr = trend(sym)
                row = {"symbol": sym, "side": side, "theme": ev["theme"], "label": ev["label"],
                       "direction": ev["direction"], "event_time": ev["gate1"]["time"] or ev["first"],
                       "clock_from": start.strftime("%Y-%m-%d %H:%M"), "gate1": ev["gate1"]["why"],
                       "gate2": ev["gate2"]["why"], "gate3": g3, "daily_trend": tr,
                       "fno": {"lot": fno.get(sym), "banned": sym in ban} if fno.get(sym) else None,
                       "tags": im.route(side, swing=(side == "long" and (ev["persistent"] or tr == "bullish")),
                                        fno_listed=bool(fno.get(sym)), fno_banned=sym in ban,
                                        multi_day_bearish=(tr == "bearish"))}
                if g3["status"] == "confirmed":
                    (longs if side == "long" else shorts).append(row)
                elif g3["status"] == "wrong way":
                    caution.append({"symbol": sym, "why": "%s: %s" % (ev["label"], g3["why"]), "event_time": row["event_time"],
                                    "theme": ev["theme"], "gates_1_2": True})
                else:
                    row["status"] = g3["status"]
                    ev.setdefault("waiting", []).append(row)
                    if args.keep_pending:
                        (longs if side == "long" else shorts).append(row)
    return {"created_by": "trader-scanner global_macro_engine", "checked": now.strftime("%Y-%m-%d %H:%M"),
            "map": map_path, "headlines": len(arts), "proxy_moves": moves, "events": events,
            "actionable_long": longs, "actionable_short": shorts, "caution": caution, "notes": notes}


def render(doc):
    out = ["Global macro & company events, %s IST: %d headline(s), %d event(s)" % (doc["checked"], doc["headlines"],
                                                                                 len(doc["events"]))]
    mv = ", ".join("%s %s" % (k, "%+.2f%%" % v if v is not None else "n/a") for k, v in doc["proxy_moves"].items())
    if mv:
        out.append("  proxies: " + mv)
    for ev in doc["events"]:
        ok = "PASS" if ev["passed_gates_1_2"] else "fail"
        out.append("  %s %-34s %-4s gate1 %s; gate2 %s" % (ok, "%s (%s)" % (ev["label"], ev["kind"]), ev["direction"],
                                                          ev["gate1"]["why"][:70], ev["gate2"]["why"][:60]))
        for w in ev.get("waiting") or []:
            out.append("        %-11s %-5s gate3 %s: %s" % (w["symbol"], w["side"], w["gate3"]["status"], w["gate3"]["why"][:80]))
    for key, title in (("actionable_long", "[ACTIONABLE LONG]"), ("actionable_short", "[ACTIONABLE SHORT]")):
        rows = doc[key]
        out.append("%s %d" % (title, len(rows)))
        for r in rows:
            out.append("  %-11s %s  %s; %s" % (r["symbol"], " ".join(r["tags"]), r["label"], r["gate3"]["why"]))
    out.append("[CAUTION / BLOCK ENTRY] %d" % len(doc["caution"]))
    for c in doc["caution"]:
        out.append("  %-11s %s" % (c["symbol"], c["why"][:110]))
    for n in doc["notes"]:
        out.append("  note: %s" % n)
    out.append("Macro maps are editable defaults, not findings. Next: order_block_scanner.py, then brief.py.")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Global macro news & equity-first NSE trade matrix (trader-scanner).")
    ap.add_argument("--themes", help="comma-separated themes from the map (default all)")
    ap.add_argument("--hours", type=int, default=3, help="headline window in hours (3)")
    ap.add_argument("--map", help="a macro_map.json (default trader/macro_map.json, else this folder's)")
    ap.add_argument("--base", default="trader")
    ap.add_argument("--no-gdelt", action="store_true")
    ap.add_argument("--no-fno", action="store_true")
    ap.add_argument("--keep-pending", action="store_true", help="list gate-3 pending stocks in the actionable lists")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-24 10:40"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers and charts from DIR")
    a = ap.parse_args(argv)
    ih.pin_clock(a.now)
    notes = []
    doc = run(a, notes)
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        sys.stdout.write(render(doc))
    if not a.dry_run:
        ih.write_json(os.path.join(a.out, "latest.json"), doc)
    return 0 if (doc["actionable_long"] or doc["actionable_short"]) else 1


if __name__ == "__main__":
    sys.exit(main())
