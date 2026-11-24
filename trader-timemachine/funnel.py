#!/usr/bin/env python3
"""The Funnel (trader-timemachine): every screener in one scored pipeline, catalyst first. No language model.

    python3 funnel.py                          # score today's candidates -> trader/funnel/latest.json + the journal
    python3 funnel.py --refresh regime,macro   # run those tools first (also: orders, news, smartmoney)
    python3 funnel.py review                   # what happened to past signals, by verdict and by stage
    python3 funnel.py --offline DIR --now "2026-10-23 10:41" --dry-run

Stages (points; any stage can veto):
  1 universe & safety (0-20)   liquidity (median turnover from trader/history/events.sqlite, else the last
                               bhavcopy): Rs 100 cr+ 10, 25 cr+ 8, 10 cr+ 5, else 2; under Rs 5 cr a day vetoed.
                               Vetoes: trade-for-trade series BE (no intraday, no shorts), ASM / GSM, a price band of
                               5 % or less. Results due tonight vetoes overnight holds only. Regime alignment
                               (regime.py): with the tilt 10, neutral 5, against it 0.
  2 catalyst & smart money (0-40)   the evidence and its DIRECTION: order wins by size against market cap
                               (10 %+ 25, 5 %+ 20, 2 %+ 12, smaller 6, unknown 8), cancellations / delays / debarments
                               (short side), results 20 % or more on last year either way (15), buybacks (8), smart
                               money (institutional or promoter buying 10; promoter selling, pledge invoked 10 on the
                               short side), the macro engine's gate-passed list (10), and the event study's
                               out-of-sample verdict for that filing type (edge +10, failed or none -5). The side with
                               more points wins; the other side's points count against it.
  3 structure (0-25)           order_block_scanner.py on the survivors only: an entry on the catalyst's side
                               (confirmation 10, VWAP held 5, open interest agrees 5, a fresh signal 5). A setup
                               against the catalyst scores 0; one blocked by the anti-trap guards is vetoed; no
                               trigger yet is WAIT.
  4 Time Machine (0-15)        brief.py's simulation after costs: expectancy 0.3R or more 15 (scaled below); no edge
                               after costs, or too few past sessions, is vetoed.
Verdict: PASS at 60 points or more with no veto; WAIT when only the trigger is missing; FAIL otherwise. Nothing
is hidden: failed candidates are listed with their reasons (warnings, as everywhere in this pack).

Journal: every candidate of every run goes to trader/journal/signals.csv (once a day per symbol and side).
`review` scores them later from NSE's daily bars: the signal day's close (the intraday exit), day 1, 3 and 5,
and whether the stop or the target came first, grouped by verdict and by the stage that vetoed. That is how
you learn which stage adds value. History, not a forecast. Nothing here places an order.
"""

import argparse
import csv
import datetime as _dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import india_bars as ib  # noqa: E402
import india_http as ih  # noqa: E402
import india_market as im  # noqa: E402
import india_signals  # noqa: E402
import events as ev  # noqa: E402
import brief  # noqa: E402

PASS_SCORE = 60
MAX = {"universe": 20, "catalyst": 40, "structure": 25, "timemachine": 15}
LIQ_MIN = 5.0
OUT_DIR = os.path.join("trader", "funnel")
JOURNAL = os.path.join("trader", "journal", "signals.csv")
OUTCOMES = os.path.join("trader", "journal", "outcomes.csv")
JOURNAL_FIELDS = ("id", "date", "time", "symbol", "side", "verdict", "total", "universe", "catalyst", "structure",
                  "timemachine", "veto_stage", "vetoes", "mode", "tf", "entry", "stop", "target", "expectancy_R",
                  "tags", "why")
OUTCOME_FIELDS = ("id", "date", "symbol", "side", "verdict", "veto_stage", "entry", "d0_pct", "d1_pct", "d3_pct",
                  "d5_pct", "first_hit", "sessions")
SCRIPTS = {"regime": ("trader-scanner", "regime.py"), "macro": ("trader-scanner", "global_macro_engine.py"),
           "orders": ("trader-news", "govt_orders_tracker.py"), "news": ("trader-news", "news.py"),
           "smartmoney": ("trader-pelosi", "smartmoney.py")}


def _load(path):
    return ih.load_json(path) or {}


def _sib(folder, script):
    return os.path.join(os.path.dirname(HERE), folder, script)


# ---------------------------------------------------------------- stage 2: evidence

def order_points(impact):
    if impact is None:
        return 8
    return 25 if impact >= 10 else 20 if impact >= 5 else 12 if impact >= 2 else 6


def evidence_for(sym, orders, signals, macro, now, days=2):
    """[{side: +1 / -1, points, why, link, etype}] for one symbol."""
    since = (now - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out, links = [], set()
    for it in orders:
        if (it.get("symbol") or "").upper() != sym or (it.get("time") or "") < since:
            continue
        n, kind = it.get("numbers") or {}, it.get("kind")
        imp = n.get("impact_pct")
        e = None
        if kind == "award":
            e = (1, order_points(imp), "order win %s%s" % ("Rs %s cr" % n["value_cr"] if n.get("value_cr") else "",
                                                             " (%.1f%% of market cap)" % imp if imp is not None else ""))
        elif kind in ("cancel", "delay"):
            e = (-1, max(10, order_points(imp)), "order %s" % ("cancelled" if kind == "cancel" else "delayed / LD"))
        elif kind == "debar":
            e = (-1, 15, "debarment")
        elif kind == "milestone":
            e = (1, 5, "order milestone")
        elif kind == "results":
            yoy = [v for k, v in n.items() if k in ("profit_yoy", "revenue_yoy") and isinstance(v, (int, float))]
            if yoy and max(abs(v) for v in yoy) >= 20:
                e = (1 if sum(yoy) > 0 else -1, 15, "results %s (press)" % ", ".join("%+.0f%%" % v for v in yoy))
        if e:
            out.append({"side": e[0], "points": e[1], "why": e[2], "link": it.get("link"),
                        "etype": it.get("event_type")})
            links.add(it.get("link"))
    for it in signals:
        if it["symbol"] != sym or (it["time"] or "") < since or it["link"] in links:
            continue
        text = ("%s %s %s" % (it["title"], it["detail"], " ".join(it["flags"]))).lower()
        e = None
        if it["kind"] == "catalyst":
            n = it["numbers"] or {}
            yoy = [v for k, v in n.items() if k in ("profit_yoy", "revenue_yoy") and isinstance(v, (int, float))]
            if "results" in it["tags"] and yoy and max(abs(v) for v in yoy) >= 20:
                e = (1 if sum(yoy) > 0 else -1, 15, "results %s" % ", ".join("%+.0f%%" % v for v in yoy))
            elif "buyback" in it["tags"] and "buy" in text and "back" in text:
                e = (1, 8, "buyback")
            elif "orders" in it["tags"] and it["source"] != "orders ledger":
                e = (1, 8, "order / contract filing")
        else:
            flags = " ".join(it["flags"]).lower()
            if "promoter" in flags and ("sale" in flags or "sell" in flags or " sold " in text):
                e = (-1, 10, "promoter selling")
            elif "invoked" in flags:
                e = (-1, 10, "pledge invoked")
            elif it["highlight"] and ("institution" in flags or "insider open-market buy" in flags
                                      or "crossed 5%" in flags):
                e = (1, 10, "smart money buying")
        if e:
            out.append({"side": e[0], "points": e[1], "why": e[2], "link": it["link"],
                        "etype": ev.classify_item(it)})
            links.add(it["link"])
    for key, side in (("actionable_long", 1), ("actionable_short", -1)):
        for r in macro.get(key) or []:
            if r.get("symbol") == sym:
                out.append({"side": side, "points": 10, "why": "macro %s %s (3 gates passed)" % (r.get("label"),
                                                                                              r.get("direction")),
                            "link": "", "etype": None})
    return out


class Odds(object):
    """The event study's out-of-sample verdict for a filing type and side (cached per run)."""

    def __init__(self, db, before):
        self.con = ev.connect(db) if db and os.path.exists(db) else None
        self.before, self.cache = before, {}

    def verdict(self, etype, side):
        if not self.con or not etype:
            return None
        k = (etype, side)
        if k not in self.cache:
            try:
                out, _ = ev.study(self.con, etype, side=side, sims=2000, before=self.before)
                best = (out.get("rules") or {}).get("best")
                self.cache[k] = best["verdict"] if best else None
            except Exception:
                self.cache[k] = None
        return self.cache[k]

    def close(self):
        if self.con:
            self.con.close()


def score_catalyst(evid, odds):
    """(points 0-40, direction 'long' / 'short' / None, reasons)."""
    if not evid:
        return 0, None, ["no catalyst in the last 2 days"]
    by = {1: 0, -1: 0}
    for e in evid:
        by[e["side"]] += e["points"]
    if by[1] == by[-1]:
        return 0, None, ["catalysts point both ways (%d vs %d)" % (by[1], by[-1])]
    side = 1 if by[1] > by[-1] else -1
    pts = by[side] - 0.5 * by[-side]
    why = ["%s %+d: %s" % ("long" if e["side"] > 0 else "short", e["points"] * (1 if e["side"] == side else -0.5), e["why"])
           for e in evid]
    for t in {e["etype"] for e in evid if e["side"] == side and e["etype"]}:
        v = odds.verdict(t, side)
        if v == "edge":
            pts += 10
            why.append("event study: %s %s has an out-of-sample edge +10" % (t, "long" if side > 0 else "short"))
        elif v in ("none", "failed out of sample"):
            pts -= 5
            why.append("event study: %s %s: %s -5" % (t, "long" if side > 0 else "short", v))
    return max(0, min(MAX["catalyst"], round(pts))), ("long" if side > 0 else "short"), why


# ---------------------------------------------------------------- stage 1

def liquidity(sym, bhav, con):
    """(median daily turnover Rs cr, where from)."""
    if con is not None:
        rows = con.execute("SELECT turnover_cr FROM bars WHERE symbol=? ORDER BY date DESC LIMIT 20", (sym,)).fetchall()
        vals = sorted(r[0] for r in rows if r[0] is not None)
        if len(vals) >= 10:
            return vals[len(vals) // 2], "20-day median (events.sqlite)"
    row = bhav.get(sym)
    if row and row.get("turnover_cr") is not None:
        return row["turnover_cr"], "last session (bhavcopy)"
    return None, "unknown"


def stage_universe(sym, bhav, con, flags):
    """(points before regime, vetoes, notes, overnight_veto)."""
    vetoes, notes = [], []
    t, src = liquidity(sym, bhav, con)
    if t is None:
        pts = 2
        notes.append("liquidity unknown")
    else:
        pts = 10 if t >= 100 else 8 if t >= 25 else 5 if t >= 10 else 2
        notes.append("turnover Rs %.1f cr a day (%s)" % (t, src))
        if t < LIQ_MIN:
            vetoes.append("illiquid: Rs %.1f cr a day (under Rs %.0f cr)" % (t, LIQ_MIN))
    if (bhav.get(sym) or {}).get("series") == "BE":
        vetoes.append("trade-for-trade (series BE): no intraday, no short selling")
    overnight = []
    for f in flags:
        if f["flag"] == "surveillance":
            vetoes.append("surveillance (ASM / GSM): %s" % f["why"][:80])
        elif f["flag"].startswith("price band"):
            vetoes.append("%s: it can lock at the circuit" % f["flag"])
        elif f["flag"].startswith("results due"):
            overnight.append(f["why"][:90])
    return pts, vetoes, notes, overnight


def regime_points(reg, side):
    if not reg or not side:
        return 5
    r = reg.get("regime")
    if r == "neutral":
        return 5
    return 10 if (r == "risk-on") == (side == "long") else 0


# ---------------------------------------------------------------- stage 3 and 4

def run_scanner(symbols, args, notes):
    script = _sib("trader-scanner", "order_block_scanner.py")
    if not os.path.exists(script):
        notes.append("structure: trader-scanner/order_block_scanner.py not found")
        return []
    cmd = [sys.executable, script, "--symbols", ",".join(symbols), "--json", "--dry-run", "--base", args.base,
           "--tf", args.tf]
    if args.now:
        cmd += ["--now", args.now]
    if args.offline:
        cmd += ["--offline", args.offline, "--no-oi"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
        doc = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        notes.append("structure: the order block scan failed (%s)" % str(e)[:100])
        return []
    notes += ["structure: %s" % n for n in doc.get("notes") or []]
    return doc.get("setups") or []


def score_structure(setups, side):
    """(points 0-25, chosen setup or None, status 'entry' / 'wait' / 'veto', reasons)."""
    mine = [s for s in setups if side is None or s["side"] == side]
    against = [s for s in setups if side is not None and s["side"] != side and s["status"] == "entry"]
    entries = [s for s in mine if s["status"] == "entry"]
    if entries:
        def pts(s):
            p = (10 if s.get("confirmation") else 0) + (5 if s.get("vwap_ok") else 0) + 5
            if (s.get("oi") or {}).get("agrees"):
                p += 5
            return min(MAX["structure"], p)
        best = max(entries, key=pts)
        return pts(best), best, "entry", ["%s %s %s: %s, %s" % (best["tf"], best["side"], best["status"],
                                                              best.get("confirmation"), best.get("vwap_kind"))]
    blocked = [s for s in mine if s["status"] == "blocked"]
    if blocked:
        return 0, blocked[0], "veto", ["trap guard: %s" % "; ".join(blocked[0]["guards"])]
    why = []
    if against:
        why.append("structure points the other way (%s %s entry)" % (against[0]["tf"], against[0]["side"]))
    waiting = [s for s in mine if s["status"] == "waiting"]
    why.append("no trigger yet%s" % (": " + "; ".join(waiting[0]["why_not"]) if waiting else
                                     " (no unmitigated order block on this side)"))
    return 0, None, "wait", why


def score_tm(row):
    """(points 0-15, veto or None, expectancy R) from a brief row."""
    st = row["swing"] if (row["mode"] == "swing" and row.get("swing") and not row["swing"].get("error")) \
        else row.get("intraday")
    if not st or st.get("error"):
        return 0, "no simulation (%s)" % ((st or {}).get("error") or "missing"), None
    if row["mode"] == "swing" and st.get("windows", 0) < 30:
        return 0, "too few past windows (%d)" % st.get("windows", 0), st.get("expectancy_R")
    exp = st.get("expectancy_R")
    if exp is None or exp <= 0:
        return 0, "no edge after costs (%s)" % ("%+.2fR" % exp if exp is not None else "?"), exp
    return min(MAX["timemachine"], int(round(exp / 0.3 * MAX["timemachine"]))), None, exp


# ---------------------------------------------------------------- the run

def refresh(args, notes):
    for name in [x for x in (args.refresh or "").split(",") if x]:
        if name not in SCRIPTS:
            notes.append("refresh: unknown tool %s (choose %s)" % (name, ", ".join(SCRIPTS)))
            continue
        script = _sib(*SCRIPTS[name])
        cmd = [sys.executable, script] + (["--now", args.now] if args.now else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
            notes.append("refresh %s: exit %d" % (name, r.returncode))
        except subprocess.TimeoutExpired:
            notes.append("refresh %s: timed out" % name)


def universe(base, orders, signals, macro, now, days=2):
    since = (now - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    syms = []
    for it in orders:
        if it.get("symbol") and (it.get("time") or "") >= since:
            syms.append(it["symbol"].upper())
    for it in signals:
        if it["symbol"] and (it["time"] or "") >= since and (it["kind"] == "smart_money" or
                                                            set(it["tags"]) & {"results", "buyback", "orders"}):
            syms.append(it["symbol"])
    for key in ("actionable_long", "actionable_short"):
        syms += [r["symbol"] for r in macro.get(key) or []]
    wl = _load(os.path.join(base, "watchlist.json"))
    syms += [str(h["symbol"]).upper() for h in wl.get("hits") or [] if h.get("symbol")]
    out = []
    for s in syms:
        s = s[:-3] if s.endswith(".NS") else s
        if s and not s.isdigit() and s not in out:
            out.append(s)
    return out


def build(args, notes):
    now = ih.now_ist().replace(second=0, microsecond=0)
    refresh(args, notes)
    base = args.base
    orders = _load(os.path.join(base, "orders", "latest.json")).get("items") or []
    macro = _load(os.path.join(base, "macro", "latest.json"))
    reg = _load(os.path.join(base, "regime", "latest.json")) or None
    all_items = india_signals.load_all(base)
    signals = [s for s in all_items if s["source"] != "orders ledger"]
    client = ih.Client(offline=args.offline)
    try:
        bhav, bhav_day = im.last_bhav(client)
        if not bhav:
            notes.append("bhavcopy: none in the last 7 days; liquidity and series unknown")
    except Exception as e:
        bhav, bhav_day = {}, None
        notes.append("bhavcopy: %s" % str(e)[:100])
    con = ev.connect(args.events_db) if os.path.exists(args.events_db) else None
    odds = Odds(args.events_db, now.strftime("%Y-%m-%d %H:%M"))
    syms = universe(base, orders, signals, macro, now)[:args.max]
    rows = {}
    for sym in syms:
        sig = india_signals.signals_for(sym, now, 3, items=all_items)
        flags = india_signals.risk_flags(sig, now)
        u_pts, vetoes, unotes, overnight = stage_universe(sym, bhav, con, flags)
        evid = evidence_for(sym, orders, signals, macro, now)
        c_pts, side, cwhy = score_catalyst(evid, odds)
        rows[sym] = {"symbol": sym, "side": side, "scores": {"universe": u_pts, "catalyst": c_pts, "structure": 0,
                                                             "timemachine": 0},
                     "vetoes": [("universe", v) for v in vetoes], "overnight_veto": overnight,
                     "why": {"universe": unotes, "catalyst": cwhy, "structure": [], "timemachine": []},
                     "setup": None, "status": None}
    odds.close()
    if con:
        con.close()
    # stage 3: the structure scan, only for names that survived stages 1-2
    live = [s for s, r in rows.items() if not r["vetoes"] and (r["side"] or r["scores"]["catalyst"] == 0)]
    setups = run_scanner(live, args, notes) if live else []
    by_sym = {}
    for s in setups:
        if s["status"] != "expired":
            by_sym.setdefault(s["symbol"], []).append(s)
    picked = []
    for sym in live:
        r = rows[sym]
        pts, best, status, why = score_structure(by_sym.get(sym, []), r["side"])
        r["scores"]["structure"], r["status"] = pts, status
        r["why"]["structure"] = why
        if status == "veto":
            r["vetoes"].append(("structure", why[0]))
        if best is not None and status == "entry":
            if r["side"] is None:
                r["side"] = best["side"]
            if best["mode"] == "swing" and r["overnight_veto"]:
                r["vetoes"].append(("universe", "results due: no overnight hold (%s)" % r["overnight_veto"][0]))
                continue
            r["setup"] = best
            picked.append(dict(best, origin="funnel %s" % best["tf"], macro=None))
        r["scores"]["universe"] = min(MAX["universe"], r["scores"]["universe"] + regime_points(reg, r["side"]))
    # stage 4: the Time Machine through the brief
    bnotes = []
    bdoc = brief.build(argparse.Namespace(base=base, sims=args.sims, months=args.months, events_db=args.events_db,
                                          offline=args.offline, no_entries_after=args.no_entries_after),
                       bnotes, cands=picked) if picked else {"active": [], "caution": [], "regime": None}
    notes += ["brief: %s" % n for n in bnotes]
    by_key = {(a["symbol"], a["side"]): a for a in bdoc["active"]}
    for sym, r in rows.items():
        a = by_key.get((sym, r["side"]))
        if not a:
            continue
        pts, veto, exp = score_tm(a)
        r["scores"]["timemachine"] = pts
        r["expectancy_R"] = exp
        r["brief"] = a
        r["why"]["timemachine"] = [brief.stat_line(a["swing"] if a["mode"] == "swing" and a.get("swing")
                                                   else a["intraday"], "swing" if a["mode"] == "swing" else "intraday")]
        if veto:
            r["vetoes"].append(("timemachine", veto))
    out = []
    for r in rows.values():
        r["total"] = sum(r["scores"].values())
        if r["vetoes"]:
            r["verdict"] = "FAIL"
        elif r["status"] == "wait":
            r["verdict"] = "WAIT"
        elif r["total"] >= PASS_SCORE and r.get("brief"):
            r["verdict"] = "PASS"
        else:
            r["verdict"] = "FAIL"
            if not r["vetoes"]:
                r["vetoes"].append(("total", "score %d under %d" % (r["total"], PASS_SCORE)))
        out.append(r)
    order = {"PASS": 0, "WAIT": 1, "FAIL": 2}
    out.sort(key=lambda r: (order[r["verdict"]], -r["total"]))
    counts = {"candidates": len(rows),
              "after_universe": sum(1 for r in rows.values() if not [v for v in r["vetoes"] if v[0] == "universe"]),
              "with_direction": sum(1 for r in rows.values() if r["side"]),
              "with_entry": sum(1 for r in rows.values() if r["setup"]),
              "passed": sum(1 for r in out if r["verdict"] == "PASS")}
    return {"created_by": "trader-timemachine funnel", "checked": now.strftime("%Y-%m-%d %H:%M"),
            "bhavcopy": bhav_day, "regime": bdoc.get("regime") or ({k: reg.get(k) for k in ("regime", "score",
                                                                                             "volatility")}
                                                                    if reg else None),
            "counts": counts, "rows": out, "caution": bdoc.get("caution", []), "notes": notes,
            "note": "Scores are rules, not predictions; every Time Machine figure is after costs. Suggestions only."}


# ---------------------------------------------------------------- the journal

def journal_rows(doc):
    out = []
    day = doc["checked"][:10]
    for r in doc["rows"]:
        a = r.get("brief") or {}
        out.append({"id": "%s|%s|%s" % (day, r["symbol"], r["side"] or "-"), "date": day, "time": doc["checked"][11:],
                    "symbol": r["symbol"], "side": r["side"] or "", "verdict": r["verdict"], "total": r["total"],
                    "universe": r["scores"]["universe"], "catalyst": r["scores"]["catalyst"],
                    "structure": r["scores"]["structure"], "timemachine": r["scores"]["timemachine"],
                    "veto_stage": r["vetoes"][0][0] if r["vetoes"] else "", "vetoes": [v[1] for v in r["vetoes"]],
                    "mode": a.get("mode", ""), "tf": a.get("tf", ""), "entry": a.get("entry", ""),
                    "stop": a.get("stop", ""), "target": a.get("target1", ""),
                    "expectancy_R": r.get("expectancy_R", ""), "tags": a.get("tags", []),
                    "why": r["why"]["catalyst"][:3]})
    return out


def write_journal(rows, path=JOURNAL):
    seen = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            seen = {r["id"] for r in csv.DictReader(fh)}
    new = [r for r in rows if r["id"] not in seen]
    ih.append_csv(path, new, JOURNAL_FIELDS)
    return len(new)


def outcome(row, daily, cost_pct=0.0):
    """What happened after a journal row: % moves (side-adjusted) from the entry (or the signal day's close)."""
    side = -1 if row["side"] == "short" else 1
    d = row["date"]
    day0 = [b for b in daily if b["date"] == d]
    after = [b for b in daily if b["date"] > d]
    entry = ih.num(row.get("entry")) or (day0[0]["c"] if day0 else None)
    if not entry or not after and not day0:
        return None

    def mv(px):
        return round(side * (px / entry - 1) * 100 - cost_pct, 3)
    res = {"id": row["id"], "date": d, "symbol": row["symbol"], "side": row["side"], "verdict": row["verdict"],
           "veto_stage": row.get("veto_stage", ""), "entry": entry,
           "d0_pct": mv(day0[0]["c"]) if day0 else "", "sessions": len(after)}
    for k, n in (("d1_pct", 1), ("d3_pct", 3), ("d5_pct", 5)):
        res[k] = mv(after[n - 1]["c"]) if len(after) >= n else ""
    stop, target = ih.num(row.get("stop")), ih.num(row.get("target"))
    first = ""
    if stop and target:
        for b in after[:5]:
            hs = (b["l"] <= stop) if side > 0 else (b["h"] >= stop)
            ht = (b["h"] >= target) if side > 0 else (b["l"] <= target)
            if hs:
                first = "stop"
                break
            if ht:
                first = "target"
                break
    res["first_hit"] = first
    return res


def review(args):
    if not os.path.exists(args.journal):
        print("funnel.py review: no journal yet (%s)" % args.journal)
        return 1
    with open(args.journal, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("side")]
    today = ih.now_ist().strftime("%Y-%m-%d")
    done = {}
    if os.path.exists(args.outcomes):
        with open(args.outcomes, newline="", encoding="utf-8") as fh:
            done = {r["id"]: r for r in csv.DictReader(fh)}
    bars, results = {}, dict(done)
    for r in rows:
        if r["date"] >= today or (r["id"] in done and done[r["id"]].get("d5_pct") not in ("", None)):
            continue
        if r["symbol"] not in bars:
            try:
                bars[r["symbol"]] = ib.nse_daily(r["symbol"], 2, offline=args.offline)
            except Exception as e:
                print("  %s: no daily bars (%s)" % (r["symbol"], str(e)[:80]))
                bars[r["symbol"]] = []
        o = outcome(r, bars[r["symbol"]])
        if o:
            results[r["id"]] = o
    if not args.dry_run and results:
        d = os.path.dirname(args.outcomes)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.outcomes, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTCOME_FIELDS, extrasaction="ignore")
            w.writeheader()
            for o in results.values():
                w.writerow(o)
    groups = {}
    for o in results.values():
        groups.setdefault(("verdict", o["verdict"]), []).append(o)
        if o.get("veto_stage"):
            groups.setdefault(("vetoed at", o["veto_stage"]), []).append(o)
    print("Funnel review %s: %d scored signals" % (today, len(results)))
    print("  %-26s %4s %9s %9s %9s %8s %8s" % ("group", "n", "day0", "day1", "day5", "target", "stop"))
    for (k, v), os_ in sorted(groups.items()):
        def avg(f):
            xs = [float(o[f]) for o in os_ if o.get(f) not in ("", None)]
            return ("%+.2f%%" % (sum(xs) / len(xs))) if xs else "-"
        hits = [o.get("first_hit") for o in os_]
        print("  %-26s %4d %9s %9s %9s %8d %8d" % ("%s %s" % (k, v), len(os_), avg("d0_pct"), avg("d1_pct"),
                                                   avg("d5_pct"), hits.count("target"), hits.count("stop")))
    print("Moves are side-adjusted from the entry (or the signal day's close), before costs. A vetoed group that lost "
          "money is a veto earning its keep. History, not a forecast.")
    return 0


# ---------------------------------------------------------------- output

def render(doc):
    c = doc["counts"]
    rg = doc.get("regime")
    out = ["Funnel %s IST: %d candidates -> %d past universe -> %d with a direction -> %d with an entry -> %d PASS" % (
        doc["checked"], c["candidates"], c["after_universe"], c["with_direction"], c["with_entry"], c["passed"]),
        "Regime: %s" % ("%s (score %+d), volatility %s" % (rg["regime"].upper(), rg["score"], rg["volatility"])
                        if rg else "unknown (run regime.py)"), ""]
    for r in doc["rows"]:
        s = r["scores"]
        out.append("%-4s %-11s %-5s %3d  [universe %2d  catalyst %2d  structure %2d  TM %2d]%s" % (
            r["verdict"], r["symbol"], r["side"] or "-", r["total"], s["universe"], s["catalyst"], s["structure"],
            s["timemachine"], ("  " + " ".join(r["brief"]["tags"])) if r.get("brief") else ""))
        for stage in ("universe", "catalyst", "structure", "timemachine"):
            for w in r["why"][stage][:3]:
                out.append("       %-10s %s" % (stage[:10], w[:120]))
        for st, v in r["vetoes"]:
            out.append("       x %-8s %s" % (st, v[:120]))
        if r.get("brief"):
            b = r["brief"]
            out.append("       entry %.2f, stop %.2f, T1 %.2f%s" % (b["entry"], b["stop"], b["target1"],
                                                                     ("; zone %.2f-%.2f" % tuple(b["zone"])) if b.get("zone") else ""))
    if not doc["rows"]:
        out.append("No candidates: run govt_orders_tracker.py, news.py, smartmoney.py or global_macro_engine.py first.")
    if doc["caution"]:
        out.append("")
        out.append("[CAUTION / DO NOT ENTER] %d" % len(doc["caution"]))
        for cz in doc["caution"][:15]:
            out.append("   %-11s %-26s %s" % (cz["symbol"], cz["kind"], cz["why"][:90]))
    for n in doc["notes"]:
        out.append("   note: %s" % n[:160])
    out.append(doc["note"])
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="The funnel: every screener in one scored pipeline (trader-timemachine).")
    ap.add_argument("command", nargs="?", default="run", choices=("run", "review"))
    ap.add_argument("--base", default="trader")
    ap.add_argument("--refresh", help="run these first: regime,macro,orders,news,smartmoney")
    ap.add_argument("--tf", default="5m,15m,60m,1d", help="order-block timeframes")
    ap.add_argument("--max", type=int, default=40, help="candidates at most")
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--events-db", default=ev.DB_PATH)
    ap.add_argument("--no-entries-after", default="14:50")
    ap.add_argument("--timeout", type=int, default=900, help="seconds for each tool it runs")
    ap.add_argument("--journal", default=JOURNAL)
    ap.add_argument("--outcomes", default=OUTCOMES)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="write nothing (no funnel file, no journal)")
    ap.add_argument("--now", help='pin the clock, India time: "2026-10-23 10:41"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers and charts from DIR")
    a = ap.parse_args(argv)
    ih.pin_clock(a.now)
    if a.command == "review":
        return review(a)
    notes = []
    doc = build(a, notes)
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        sys.stdout.write(render(doc))
    if not a.dry_run:
        ih.write_json(os.path.join(a.out, "latest.json"), doc)
        n = write_journal(journal_rows(doc), a.journal)
        if not a.json:
            print("Journal: %d new signal(s) in %s; score them later with funnel.py review." % (n, a.journal))
    return 0 if doc["counts"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
