#!/usr/bin/env python3
"""Market Regime (trader-scanner): is the Indian market risk-on, neutral or risk-off today, and how volatile?
Five official NSE inputs, plain scoring, no language model, no forecast.

    python3 regime.py                       # -> trader/regime/latest.json (run before the open and after 15:30)
    python3 regime.py --json
    python3 regime.py --offline DIR --now "2026-11-24 08:45" --dry-run

Inputs (each scores -1, 0 or +1; the sum decides):
  volatility    India VIX from NSE's market mood (get_market_mood): its percentile over the last year and its
                1-week change. 80th percentile or more, or up 20 % or more in a week: -1 (fear rising);
                30th percentile or less and not rising: +1.
  breadth       the share of NSE stocks that rose on the day (market mood): 60 % or more +1, 40 % or less -1.
  trend         Nifty 50 against its 20- and 50-day averages (get_index_history): above both with the 20 over
                the 50 +1, below both with the 20 under the 50 -1.
  positioning   FII index futures, long / (long + short) contracts, from NSE's participant-wise open interest
                (nsearchives fao_participant_oi_<DDMMYYYY>.csv): 60 % or more +1, 35 % or less -1.
  flows         FII / FPI net cash buying (NSE fiidiiTradeReact, provisional): Rs 2,000 cr or more +1,
                -2,000 cr or less -1 (DII shown alongside).
Regime: score +2 or more risk-on, -2 or less risk-off, otherwise neutral. Volatility high (VIX 80th
percentile or more) says: halve the size. The brief and the trading tools show it as a warning; it never
blocks anything. A regime is a description of today, not a prediction of tomorrow.
"""

import argparse
import csv
import datetime as _dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_http as ih  # noqa: E402
import nse_data  # noqa: E402

OUT_DIR = os.path.join("trader", "regime")
POI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_%s.csv"


# ---------------------------------------------------------------- scoring (pure)

def score_volatility(mood):
    v = (mood or {}).get("india_vix") or {}
    level, pctl = v.get("level"), (v.get("range") or {}).get("percentile")
    wk = (v.get("vs_1w_ago") or {}).get("change_pct")
    if level is None:
        return None
    if (pctl is not None and pctl >= 80) or (wk is not None and wk >= 20):
        s, read = -1, "fear rising"
    elif pctl is not None and pctl <= 30 and (wk is None or wk <= 0):
        s, read = 1, "calm"
    else:
        s, read = 0, "normal"
    return {"name": "volatility", "score": s, "reading": read,
            "value": "India VIX %.2f (%s percentile of the last year, %s on the week)" % (
                level, "%.0fth" % pctl if pctl is not None else "?", "%+.1f%%" % wk if wk is not None else "?"),
            "vix": level, "percentile": pctl, "week_change_pct": wk}


def score_breadth(mood):
    b = (mood or {}).get("stock_breadth") or {}
    up = b.get("up_pct")
    if up is None:
        return None
    s = 1 if up >= 60 else -1 if up <= 40 else 0
    return {"name": "breadth", "score": s, "reading": b.get("reading") or "",
            "value": "%.0f%% of %s stocks rose (%s up, %s down)" % (up, b.get("stocks", "?"), b.get("up"), b.get("down")),
            "up_pct": up}


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def score_trend(closes):
    if len(closes) < 50:
        return None
    last, m20, m50 = closes[-1], sma(closes, 20), sma(closes, 50)
    if last > m20 and last > m50 and m20 > m50:
        s, read = 1, "uptrend"
    elif last < m20 and last < m50 and m20 < m50:
        s, read = -1, "downtrend"
    else:
        s, read = 0, "mixed"
    return {"name": "trend", "score": s, "reading": read,
            "value": "Nifty 50 %.2f; 20-day %.2f, 50-day %.2f" % (last, m20, m50), "nifty": last,
            "sma20": round(m20, 2), "sma50": round(m50, 2)}


def parse_participant_oi(text):
    """NSE's participant-wise OI CSV -> {Client / DII / FII / Pro: {column: contracts}}."""
    rows = list(csv.reader(io.StringIO((text or "").replace("\r", ""))))
    head = None
    out = {}
    for r in rows:
        cells = [c.strip() for c in r]
        if cells and cells[0] == "Client Type":
            head = cells
            continue
        if head and cells and cells[0] in ("Client", "DII", "FII", "Pro", "TOTAL"):
            out[cells[0]] = {h: ih.num(v) for h, v in zip(head[1:], cells[1:])}
    return out


def score_positioning(poi, prev=None):
    f = (poi or {}).get("FII") or {}
    lo, sh = f.get("Future Index Long"), f.get("Future Index Short")
    if not lo and not sh:
        return None
    pct = lo / (lo + sh) * 100
    s = 1 if pct >= 60 else -1 if pct <= 35 else 0
    chg = ""
    pf = (prev or {}).get("FII") or {}
    if pf.get("Future Index Long") is not None and pf.get("Future Index Short") is not None:
        p0 = pf["Future Index Long"] / (pf["Future Index Long"] + pf["Future Index Short"]) * 100
        chg = " (%+.1f pp on the day)" % (pct - p0)
    return {"name": "positioning", "score": s, "reading": "FII net long" if s > 0 else "FII net short" if s < 0
            else "FII balanced", "value": "FII index futures %.0f%% long (%s long, %s short contracts)%s" % (
                pct, "{:,.0f}".format(lo), "{:,.0f}".format(sh), chg), "fii_index_long_pct": round(pct, 1)}


def score_flows(rows):
    by = {str(r.get("category") or "").upper(): r for r in rows or [] if isinstance(r, dict)}
    fii = by.get("FII/FPI") or by.get("FII")
    if not fii:
        return None
    net = ih.num(fii.get("netValue"))
    dii = ih.num((by.get("DII") or {}).get("netValue"))
    if net is None:
        return None
    s = 1 if net >= 2000 else -1 if net <= -2000 else 0
    return {"name": "flows", "score": s, "reading": "FII buying" if s > 0 else "FII selling" if s < 0 else "FII flat",
            "value": "FII / FPI net Rs %s cr, DII net Rs %s cr (%s, provisional)" % (
                "{:,.0f}".format(net), "{:,.0f}".format(dii) if dii is not None else "?", fii.get("date")),
            "fii_net_cr": net, "dii_net_cr": dii}


def regime(components):
    comps = [c for c in components if c]
    total = sum(c["score"] for c in comps)
    name = "risk-on" if total >= 2 else "risk-off" if total <= -2 else "neutral"
    vol = next((c for c in comps if c["name"] == "volatility"), None)
    high_vol = bool(vol and vol.get("percentile") is not None and vol["percentile"] >= 80)
    advice = {"risk-on": "the tape is with longs; shorts fight it",
              "risk-off": "the tape is against longs: fewer, smaller longs; shorts and caution lists matter more",
              "neutral": "no strong tilt: take the setups on their own merits"}[name]
    if high_vol:
        advice += "; volatility is high: halve the size (stops get hit by noise)"
    return {"regime": name, "score": total, "of": len(comps), "volatility": "high" if high_vol else "normal",
            "size_hint": "half" if high_vol or name == "risk-off" else "full", "advice": advice, "components": comps}


# ---------------------------------------------------------------- fetching

def _mcp(url, tool, args, offline, name):
    if offline:
        with open(os.path.join(offline, name), encoding="utf-8") as fh:
            return json.load(fh)
    ok, payload = nse_data.call_tools(url, [(tool, args)])[0]
    if not ok:
        raise ih.FeedError("%s: %s" % (tool, payload))
    return payload


def nifty_closes(offline, months=4):
    doc = _mcp(nse_data.EOD_URL, "get_index_history", {"indexName": "Nifty 50", "months": months, "endDate": "today"},
               offline, "nifty50.json")
    rows = sorted((r for r in (doc or {}).get("data") or [] if r.get("close") is not None), key=lambda r: r["date"])
    return [float(r["close"]) for r in rows], (rows[-1]["date"] if rows else None)


def participant_oi(client, day, back=7):
    """(this file, the one before, its date) going back from day."""
    got = []
    d = day
    for _ in range(back * 2):
        if len(got) == 2:
            break
        stamp = d.strftime("%d%m%Y")
        try:
            got.append((parse_participant_oi(client.get_text(POI % stamp, "fao_participant_oi_%s.csv" % stamp)),
                        d.isoformat()))
        except ih.FeedError:
            pass
        d -= _dt.timedelta(days=1)
    if not got:
        return None, None, None
    return got[0][0], (got[1][0] if len(got) > 1 else None), got[0][1]


def build(args, notes):
    now = ih.now_ist()
    client = ih.Client(offline=args.offline)
    comps = []

    def attempt(name, fn):
        try:
            c = fn()
            if c is None:
                notes.append("%s: no data" % name)
            comps.append(c)
        except Exception as e:  # one input failing never stops the rest
            notes.append("%s: %s" % (name, str(e)[:140]))
    mood = {}
    try:
        mood = _mcp(nse_data.EOD_URL, "get_market_mood", {"date": "today"}, args.offline, "market_mood.json")
    except Exception as e:
        notes.append("market mood: %s" % str(e)[:140])
    attempt("volatility", lambda: score_volatility(mood))
    attempt("breadth", lambda: score_breadth(mood))

    def trend():
        closes, _ = nifty_closes(args.offline)
        return score_trend(closes)
    attempt("trend", trend)

    def positioning():
        cur, prev, day = participant_oi(client, now.date())
        c = score_positioning(cur, prev)
        if c:
            c["as_of"] = day
        return c
    attempt("positioning", positioning)
    attempt("flows", lambda: score_flows(client.nse_api("/api/fiidiiTradeReact", "nse_fiidii.json")))
    doc = regime(comps)
    doc.update({"created_by": "trader-scanner regime", "checked": now.strftime("%Y-%m-%d %H:%M"),
                "market_date": mood.get("date"), "notes": notes,
                "note": "A description of today's market from NSE's own data, not a forecast. Warnings only."})
    return doc


def render(doc):
    out = ["Market regime %s IST (NSE data for %s): %s, score %+d of %d inputs; volatility %s" % (
        doc["checked"], doc.get("market_date") or "?", doc["regime"].upper(), doc["score"], doc["of"], doc["volatility"])]
    for c in doc["components"]:
        out.append("  %-12s %+d  %-16s %s" % (c["name"], c["score"], c["reading"], c["value"]))
    out.append("  -> %s (size: %s)" % (doc["advice"], doc["size_hint"]))
    for n in doc["notes"]:
        out.append("  note: %s" % n)
    out.append(doc["note"])
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Market regime from NSE data (trader-scanner).")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-24 08:45"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers: market_mood.json, nifty50.json, "
                                                     "nse_fiidii.json, fao_participant_oi_<DDMMYYYY>.csv")
    a = ap.parse_args(argv)
    ih.pin_clock(a.now)
    notes = []
    doc = build(a, notes)
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        sys.stdout.write(render(doc))
    if not a.dry_run:
        ih.write_json(os.path.join(a.out, "latest.json"), doc)
    return 0 if doc["of"] else 1


if __name__ == "__main__":
    sys.exit(main())
