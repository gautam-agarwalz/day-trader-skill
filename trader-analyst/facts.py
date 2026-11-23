#!/usr/bin/env python3
"""Earnings reader for trader-analyst: a company's reported quarters from NSE's own filings, and
what changed. No language model: JSON and XBRL parsing and arithmetic only.

    python3 facts.py TCS
    python3 facts.py TCS INFY --quarters 8
    python3 facts.py RELIANCE --standalone --json
    python3 facts.py TCS INFY HDFCBANK --metrics trader/metrics.json

Where the numbers come from (official NSE, see india_results.py):
  - every results filing under SEBI's integrated filing (from early 2025), and each filing's XBRL:
    revenue from operations, other income, total income, expenses, finance costs, depreciation,
    profit before tax, profit for the period, basic and diluted EPS, quarter and year to date
  - NSE's older results-comparison feed for quarters before 2025 (standalone figures only, so
    they are used for --standalone comparisons)
  - the company's board meetings: when the next results will be considered

Consolidated figures by default (the group), standalone with --standalone or when a company files
no consolidated results. Rupees are shown in crore. A quarter is compared with the same quarter a
year earlier (YoY) and with the quarter before (QoQ); TTM is the last four quarters added up.
Banks and NBFCs file some lines under other names; a line a filing does not carry is left blank.

--metrics FILE merges one row per symbol into a JSON file. --offline DIR reads saved answers
(the tests use it). Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_http as ih  # noqa: E402
import india_results as ir  # noqa: E402

MAX_XBRL = 12   # XBRL files read per symbol per run (a quarter each)


def load_quarters(client, symbol, quarters=6, standalone=False, notes=None):
    """-> (quarters newest first, the filing rows used). Each quarter: {period_start, period_end,
    quarter: {field: rupees}, nature, source, filing}."""
    notes = notes if notes is not None else []
    nature = "Standalone" if standalone else "Consolidated"
    rows = ir.filings(client.nse_api(ir.INTEGRATED + "&symbol=" + symbol, "nse_results_%s.json" % ih.safe_name(symbol)))
    picks = ir.pick_quarters(rows, nature)
    out = []
    for f in picks[:min(MAX_XBRL, quarters + 4)]:
        try:
            x = ir.parse_results_xbrl(client.get_text(f["xbrl"], ih.safe_name(f["xbrl"].rsplit("/", 1)[-1])))
        except ih.FeedError as e:
            notes.append("%s %s: %s" % (symbol, f["quarter_end"], e))
            continue
        if not x["quarter"]:
            notes.append("%s %s: the XBRL has no quarter figures" % (symbol, f["quarter_end"]))
            continue
        out.append({"period_start": x["period_start"], "period_end": x["period_end"], "quarter": x["quarter"],
                    "ytd": x.get("ytd"), "nature": x["nature"] or f["nature"], "source": "NSE integrated filing",
                    "filing": f, "company": x["company"]})
    natures = {q["nature"] for q in out}
    if out and nature not in natures:
        notes.append("%s files %s results only" % (symbol, "/".join(sorted(natures)).lower()))
    if standalone or not out or natures == {"Standalone"}:
        try:
            older = ir.comparison_quarters(client.nse_api(ir.COMPARISON % symbol, "nse_compare_%s.json" % ih.safe_name(symbol)))
            have = {q["period_end"] for q in out}
            out += [q for q in older if q["period_end"] not in have]
        except ih.FeedError as e:
            notes.append("%s: NSE's older results feed: %s" % (symbol, e))
    out.sort(key=lambda q: q["period_end"], reverse=True)
    return out, picks


def analyse(quarters, n=6):
    """Pure: quarters newest first -> rows with margins, YoY, QoQ, and the TTM."""
    rows = []
    for i, q in enumerate(quarters[:n]):
        v = q["quarter"]
        prev = quarters[i + 1] if i + 1 < len(quarters) else None
        ly = ir.same_quarter_last_year(quarters, q["period_end"])
        rows.append({
            "period_end": q["period_end"].isoformat(), "nature": q["nature"], "source": q["source"],
            "revenue_cr": ir.crore(v.get("revenue")), "total_income_cr": ir.crore(v.get("total_income")),
            "pbt_cr": ir.crore(v.get("pbt")), "profit_cr": ir.crore(v.get("profit")), "eps": v.get("eps"),
            "net_margin": ir.margin(v.get("profit"), v.get("revenue")), "pbt_margin": ir.margin(v.get("pbt"), v.get("revenue")),
            "revenue_yoy": ir.pct(v.get("revenue"), (ly or {}).get("quarter", {}).get("revenue")),
            "profit_yoy": ir.pct(v.get("profit"), (ly or {}).get("quarter", {}).get("profit")),
            "eps_yoy": ir.pct(v.get("eps"), (ly or {}).get("quarter", {}).get("eps")),
            "revenue_qoq": ir.pct(v.get("revenue"), (prev or {}).get("quarter", {}).get("revenue")),
            "profit_qoq": ir.pct(v.get("profit"), (prev or {}).get("quarter", {}).get("profit")),
            "filed": (q.get("filing") or {}).get("broadcast", ""), "xbrl": (q.get("filing") or {}).get("xbrl", ""),
        })
    ttm = None
    last4 = quarters[:4]
    if len(last4) == 4 and len({q["nature"] for q in last4}) == 1 and \
            all(270 <= (last4[0]["period_end"] - last4[3]["period_end"]).days <= 280 for _ in [0]):
        ttm = {k: ir.crore(sum(q["quarter"].get(f) or 0 for q in last4)) for k, f in
               (("revenue_cr", "revenue"), ("profit_cr", "profit"))}
        ttm["eps"] = round(sum(q["quarter"].get("eps") or 0 for q in last4), 2)
    return rows, ttm


def changes(rows):
    """Plain sentences on what changed in the latest quarter."""
    if not rows:
        return []
    r = rows[0]
    out = []
    for label, yoy, qoq in (("Revenue", r["revenue_yoy"], r["revenue_qoq"]), ("Profit", r["profit_yoy"], r["profit_qoq"])):
        bits = []
        if yoy is not None:
            bits.append("%+.1f%% on the same quarter last year" % yoy)
        if qoq is not None:
            bits.append("%+.1f%% on the quarter before" % qoq)
        if bits:
            out.append("%s %s." % (label, " and ".join(bits)))
    if r["net_margin"] is not None and len(rows) > 1 and rows[1]["net_margin"] is not None:
        out.append("Net margin %.1f%% (%.1f%% the quarter before)." % (r["net_margin"], rows[1]["net_margin"]))
    if r["profit_cr"] is not None and r["profit_cr"] < 0:
        out.append("A loss this quarter.")
    return out


def _c(x):
    return "-" if x is None else "{:,.0f}".format(x) if abs(x) >= 100 else "{:,.2f}".format(x)


def _p(x):
    return "-" if x is None else "%+.1f%%" % x


def render(symbol, company, rows, ttm, nxt, latest, notes):
    out = ["%s%s, %s quarters (Rs crore, from NSE filings)" % (symbol, (" (%s)" % company) if company else "",
                                                               rows[0]["nature"].lower() if rows else "no")]
    if rows:
        out.append("  %-10s %10s %10s %10s %7s %7s %8s %8s %8s" % ("quarter", "revenue", "profit", "PBT", "EPS",
                                                                 "net mgn", "rev YoY", "PAT YoY", "rev QoQ"))
        for r in rows:
            out.append("  %-10s %10s %10s %10s %7s %7s %8s %8s %8s" % (
                r["period_end"], _c(r["revenue_cr"]), _c(r["profit_cr"]), _c(r["pbt_cr"]),
                "-" if r["eps"] is None else "%.2f" % r["eps"], "-" if r["net_margin"] is None else "%.1f%%" % r["net_margin"],
                _p(r["revenue_yoy"]), _p(r["profit_yoy"]), _p(r["revenue_qoq"])))
        if ttm:
            out.append("  TTM (last 4 quarters): revenue Rs %s cr, profit Rs %s cr, EPS %.2f" % (
                _c(ttm["revenue_cr"]), _c(ttm["profit_cr"]), ttm["eps"]))
        for line in changes(rows):
            out.append("  " + line)
    if latest:
        out.append("  Latest results filing: quarter to %s, filed %s IST (%s, %s)%s" % (
            latest["quarter_end"], latest["broadcast"], latest["nature"].lower(), latest["audited"].lower(),
            ", revised" if latest["revised"] else ""))
        out.append("    %s" % (latest["ixbrl"] or latest["xbrl"]))
    if nxt:
        out.append("  Next results: board meeting on %s (%s)" % (nxt["date"], nxt["purpose"] or nxt["desc"][:80]))
    for n in notes:
        out.append("  note: %s" % n)
    return "\n".join(out)


def metrics_row(symbol, rows, ttm, nxt):
    r = rows[0] if rows else {}
    return {"symbol": symbol, "period_end": r.get("period_end"), "nature": r.get("nature"),
            "revenue_cr": r.get("revenue_cr"), "profit_cr": r.get("profit_cr"), "eps": r.get("eps"),
            "net_margin": r.get("net_margin"), "revenue_yoy": r.get("revenue_yoy"), "profit_yoy": r.get("profit_yoy"),
            "ttm_revenue_cr": (ttm or {}).get("revenue_cr"), "ttm_profit_cr": (ttm or {}).get("profit_cr"),
            "next_results": (nxt or {}).get("date")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Earnings reader (trader-analyst): a company's quarters from NSE filings.")
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. TCS INFY")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--standalone", action="store_true", help="standalone figures instead of consolidated")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-23 21:30"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers from DIR, no network")
    ap.add_argument("--record", metavar="DIR", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not a.symbols:
        print("facts.py: give one or more NSE symbols, e.g. facts.py TCS INFY", file=sys.stderr)
        return 2
    ih.pin_clock(a.now)
    client = ih.Client(offline=a.offline, record=a.record)
    today = ih.now_ist().date().isoformat()
    results, texts, failed = {}, [], 0
    for raw in a.symbols:
        sym = raw.strip().upper()
        sym = sym[:-3] if sym.endswith(".NS") else sym
        notes = []
        try:
            quarters, picks = load_quarters(client, sym, a.quarters, a.standalone, notes)
        except ih.FeedError as e:
            print("facts.py: %s: %s" % (sym, e), file=sys.stderr)
            failed += 1
            continue
        if not quarters:
            print("facts.py: %s: NSE has no results filings for this symbol (check the symbol)" % sym, file=sys.stderr)
            failed += 1
            continue
        rows, ttm = analyse(quarters, a.quarters)
        try:
            nxt = ir.next_results_meeting(ir.board_meetings(client.nse_api(ir.BOARD % sym, "nse_board_%s.json" % ih.safe_name(sym))), today)
        except ih.FeedError as e:
            nxt = None
            notes.append("board meetings: %s" % e)
        latest = picks[0] if picks else None
        company = quarters[0].get("company") or ""
        results[sym] = {"symbol": sym, "company": company, "quarters": rows, "ttm": ttm, "next_results": nxt,
                        "latest_filing": latest, "changes": changes(rows), "notes": notes}
        texts.append(render(sym, company, rows, ttm, nxt, latest, notes))
    if a.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif texts:
        print("\n\n".join(texts))
        print("\nReported numbers from NSE filings, not a view on the stock. Not financial advice.")
    if a.metrics and results:
        doc = ih.load_json(a.metrics) or {}
        for sym, r in results.items():
            doc[sym] = metrics_row(sym, r["quarters"], r["ttm"], r["next_results"])
        ih.write_json(a.metrics, doc)
        if not a.json:
            print("Merged %d row(s) into %s." % (len(results), a.metrics))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
