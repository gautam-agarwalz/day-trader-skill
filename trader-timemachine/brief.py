#!/usr/bin/env python3
"""Universal Time Machine & Trade Brief (trader-timemachine): every candidate trade from the order block
scanner and the macro engine, intraday or a 2-3+ day swing, long or short, is run through thousands of
simulated forward paths before it is shown. Arithmetic only, no language model.

    python3 brief.py                      # trader/orderblocks + trader/macro + trader/orders -> trader/brief/
    python3 brief.py --sims 10000 --json
    python3 brief.py plan 1 --write       # setup 1 -> trader/plan.json for /trader-exit, budget, risk
    python3 brief.py --offline DIR --now "2026-10-23 13:05" --dry-run

How each candidate is simulated:
  intraday  timemachine.time_machine(): 10,000 replays of the rest of the session drawn from the stock's own
            last 60 days of 5-minute bars (the same engine as `timemachine.py run`), long or short:
            target / stop / still open at the close, win rate (finished green), average R, MAE (how far it
            went against you before the exit, in R), and the average R for every exit time (the best one is
            the intraday exit window; trades are flat by 15:15 anyway).
  swing     events.swing_paths(): every 3-session window of the stock's own daily history (NSE bhavcopy),
            drawn 10,000 times, entered at a close and replayed bar by bar with the overnight gaps as they
            happened (a gap through the stop fills at the open): win rate, expectancy, MAE, how it ended,
            the average for exiting on day 1, 2 or 3. With trader/history/events.sqlite and a known event
            type (an order win today) the same replay also runs on other stocks' days after that event.
  both      for a long, intraday and swing are compared and the better exit window is named.
Costs come off the swing numbers (0.30 % a round trip); intraday R is before costs.

The brief:
  (a) [EQUITY LONG] / [EQUITY SHORT] setups with their routing tags ([EQUITY INTRADAY LONG],
      [EQUITY SWING LONG: 2-3+ DAYS], [EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM], and
      [SECONDARY F&O] / [SECONDARY F&O SWING SHORT OPTION] only for an F&O stock out of the ban period
      with a multi-day bearish setup): the order block zone, the 50 % mean-threshold entry, the
      confirmation entry, stop, targets, win rate, expectancy, R:R, MAE, the exit window, the sample size.
      Every setup is shown; one whose simulation loses money is labelled "no edge in the Time Machine".
  (b) [CAUTION / DO NOT ENTER]: macro headwinds (the theme's caution list), EXECUTION LAG WATCH and
      ORDER LOSS / CANCELLATION from the orders ledger, debarments, order block setups blocked by the
      anti-trap guards, the F&O ban, surveillance and results-day flags (india_signals).
History, not a forecast. Suggestions only: nothing here places an order.
"""

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_bars as ib  # noqa: E402
import india_http as ih  # noqa: E402
import india_market as im  # noqa: E402
import india_signals  # noqa: E402
import india_costs  # noqa: E402
import events as ev  # noqa: E402
import timemachine as tm  # noqa: E402

OUT_DIR = os.path.join("trader", "brief")
PLAN = os.path.join("trader", "plan.json")
SWING_DAYS = 3
SIMS = 10000
BLOCKED_WINDOWS = (("09:15", "09:30", "opening noise (09:15-09:30)"),     # order_block_scanner's anti-trap guards
                   ("11:45", "13:30", "midday fakeouts (11:45-13:30)"))
STOP_FLOOR_ATR = 0.35


def regime_flags(reg, side):
    """Warnings, never blocks: a long in a risk-off market, a short in a risk-on one, high volatility."""
    if not reg:
        return []
    out = []
    if (reg.get("regime") == "risk-off" and side == "long") or (reg.get("regime") == "risk-on" and side == "short"):
        out.append({"flag": "regime %s" % reg["regime"], "why": "a %s against the market's tilt: %s" % (
            side, reg.get("advice", "")), "link": ""})
    if reg.get("volatility") == "high":
        out.append({"flag": "high volatility", "why": "India VIX is high: half size (regime.py)", "link": ""})
    return out


def anti_trap(hhmm, no_after="14:50"):
    """The order block scanner's time guards for any intraday entry: None when the time is fine."""
    for a, z, why in BLOCKED_WINDOWS:
        if a <= hhmm <= z:
            return why
    if hhmm > no_after:
        return "after no_entries_after %s" % no_after
    return None          # a macro (non order block) entry: stop at least this x the 14-day ATR away


# ---------------------------------------------------------------- data

def daily_from_intraday(bars):
    out = {}
    for b in bars:
        d = out.get(b["date"])
        if d is None:
            out[b["date"]] = dict(b, time="")
        else:
            d["h"], d["l"], d["c"], d["v"] = max(d["h"], b["h"]), min(d["l"], b["l"]), b["c"], d["v"] + b["v"]
    return [out[k] for k in sorted(out)]


class Data(object):
    def __init__(self, args, notes, now):
        self.args, self.notes, self.now = args, notes, now
        self.now_utc = now - ib.IST
        self._five, self._daily = {}, {}
        self.client = ih.Client(offline=args.offline)

    def five_text(self, sym):
        if sym not in self._five:
            try:
                self._five[sym] = ib.fetch_text(sym, "5m", offline=self.args.offline)
            except ib.BarsError as e:
                self.notes.append("%s: 5-minute bars: %s" % (sym, e))
                self._five[sym] = None
        return self._five[sym]

    def daily(self, sym):
        if sym not in self._daily:
            today = self.now.strftime("%Y-%m-%d")
            try:
                rows = ib.nse_daily(sym, self.args.months, offline=self.args.offline)
            except Exception as e:
                text = self.five_text(sym)
                rows = daily_from_intraday(ib.parse(text, self.now_utc)) if text else []
                self.notes.append("%s: NSE daily bars unavailable (%s); %s" % (
                    sym, str(e)[:80], "using the 60 days of 5-minute bars" if rows else "no swing replay"))
            self._daily[sym] = [r for r in rows if r["date"] < today]
        return self._daily[sym]

    def turnover_cr(self, sym):
        """Median daily turnover of the last 20 sessions, Rs crore (prices the slippage), or None."""
        rows = self.daily(sym)[-20:]
        vals = sorted(r["c"] * r["v"] / 1e7 for r in rows if r.get("v"))
        return vals[len(vals) // 2] if vals else None


# ---------------------------------------------------------------- the simulations

def intraday_tm(data, s, side, entry, stop, target, sims):
    """timemachine.time_machine on today's session, or {"error": why}."""
    text = data.five_text(s["symbol"])
    if not text:
        return {"error": "no 5-minute bars"}
    day, hhmm = s["signal_time"][:10], s["signal_time"][11:16]
    plan = {"symbol": s["symbol"], "side": "buy" if side == "long" else "sell", "date": day, "entry": entry,
            "stop": stop, "target": target, "entry_time": hhmm, "setup": "order_block"}
    try:
        p = tm.normalize_plan(plan, "brief")
        doc = tm.time_machine(text, p, data.now_utc, replays=sims, until="15:15",
                              turnover_cr=data.turnover_cr(s["symbol"]))
    except tm.TMError as e:
        return {"error": str(e)}
    mc = doc["mc"]
    return {"engine": "timemachine (own 60-day 5-min sessions)", "replays": mc["replays"],
            "sessions": mc["pool_sessions"], "win_pct": round(mc["p_green"] * 100, 1),
            "p_target": round(mc["p_target"] * 100, 1), "p_stop": round(mc["p_stop"] * 100, 1),
            "p_open_at_flat": round(mc["p_neither"] * 100, 1), "expectancy_R": mc["avg_R_net"],
            "expectancy_R_gross": mc["avg_R"], "cost_R": mc["cost_R"],
            "rr": mc["r_at_target"], "mae_R": mc.get("mae_R"), "best_exit": mc.get("best_exit"),
            "median_close": mc["median_close"], "p5_close": mc["p5_close"], "p95_close": mc["p95_close"],
            "verdict": doc["verdict"], "warnings": doc["warnings"]}


def swing_tm(data, sym, side, entry, stop, target, sims, event_type=None):
    rows = data.daily(sym)
    out = {}
    product = "delivery" if side == "long" else "futures"       # an overnight short is only possible in futures
    cost = india_costs.cost_pct(product, turnover_cr=data.turnover_cr(sym))
    w = ev.windows_from_bars(rows, SWING_DAYS)
    if len(w) >= 20:
        r = ev.swing_paths(w, entry, stop, target, 1 if side == "long" else -1, SWING_DAYS, sims, cost=cost)
        out = dict(r, engine="events.swing_paths (own daily history, %d windows)" % r["windows"], product=product,
                   after_tax_pct=india_costs.after_tax(product, r["mean_net_pct"]))
    else:
        out = {"error": "only %d daily windows for %s (need 20)" % (len(w), sym)}
    if event_type and os.path.exists(data.args.events_db):
        try:
            con = ev.connect(data.args.events_db)
            cal = ev.Calendar(con)
            evs = ev.collect(con, cal, [event_type], before=data.now.strftime("%Y-%m-%d %H:%M"))
            ew = ev.windows_from_events(evs, SWING_DAYS)
            if len(ew) >= ev.MIN_EVENTS:
                r = ev.swing_paths(ew, entry, stop, target, 1 if side == "long" else -1, SWING_DAYS, sims, cost=cost)
                out["event_pool"] = dict(r, engine="%d past %s events, day-0 paths" % (len(ew), event_type))
            con.close()
        except Exception as e:  # the event pool is extra: never fatal
            data.notes.append("event pool %s: %s" % (event_type, str(e)[:100]))
    return out


def exit_window(intra, swing):
    """Name the better exit: the intraday best time, or day 1 / 2 / 3 of a swing, by average R."""
    opts = []
    if intra and not intra.get("error") and intra.get("best_exit"):
        be = intra["best_exit"]
        opts.append(("intraday by %s" % be["time"], be["avg_R"]))
    if swing and not swing.get("error"):
        sf = None
        if swing.get("expectancy_R") and swing.get("mean_net_pct"):
            sf = swing["mean_net_pct"] / swing["expectancy_R"] if swing["expectancy_R"] else None
        for d, v in sorted((swing.get("avg_net_by_day") or {}).items(), key=lambda kv: int(kv[0])):
            r = v / sf if sf else None
            if r is not None:
                opts.append(("day %s close" % d, round(r, 3)))
    if not opts:
        return None
    best = max(opts, key=lambda o: o[1])
    return {"best": best[0], "avg_R": best[1], "options": [{"exit": a, "avg_R": b} for a, b in opts]}


# ---------------------------------------------------------------- candidates

def load(path):
    return ih.load_json(path) or {}


def next_sessions(day, n):
    d = _dt.date.fromisoformat(day)
    out = []
    while len(out) < n:
        d += _dt.timedelta(days=1)
        if im.trading_day(d):
            out.append(d.isoformat())
    return out


def candidates(base, now):
    """Order block setups (all statuses) and macro gate-3 confirmations, merged per symbol and side."""
    obs = load(os.path.join(base, "orderblocks", "latest.json")).get("setups") or []
    macro = load(os.path.join(base, "macro", "latest.json"))
    out = []
    for s in obs:
        out.append(dict(s, origin="order block %s" % s["tf"], macro=None))
    for key, side in (("actionable_long", "long"), ("actionable_short", "short")):
        for m in macro.get(key) or []:
            twin = [c for c in out if c["symbol"] == m["symbol"] and c["side"] == side]
            if twin:
                for c in twin:
                    c["macro"] = m
                continue
            out.append({"symbol": m["symbol"], "tf": "5m", "mode": "swing" if any("SWING" in t for t in m["tags"])
                        else "intraday", "side": side, "status": "entry", "origin": "macro %s" % m["label"],
                        "macro": m, "signal_time": (m["gate3"].get("bar") or now.strftime("%Y-%m-%d %H:%M")),
                        "zone": None, "mean_threshold": None, "entry": None, "confirmation": "gate 3: " + m["gate3"]["why"],
                        "guards": [], "why_not": [], "daily_trend": m.get("daily_trend"), "oi": None})
    return out, macro


def macro_levels(data, c):
    """A macro entry without an order block: the gate-3 bar's close, a stop beyond that bar (at least
    0.35x the 14-day ATR), targets 1.5R and 3R."""
    text = data.five_text(c["symbol"])
    if not text:
        return False
    bars = ib.parse(text, data.now_utc)
    t = c["signal_time"]
    hit = [b for b in bars if "%s %s" % (b["date"], b["time"]) < t]     # t is the gate-3 bar's END: start < end
    if not hit:
        return False
    b = hit[-1]
    daily = data.daily(c["symbol"])
    a = ib.atr(daily, 14) or ib.atr(bars, 14) or b["c"] * 0.01
    long_ = c["side"] == "long"
    stop = min(b["l"], b["c"] - STOP_FLOOR_ATR * a) if long_ else max(b["h"], b["c"] + STOP_FLOOR_ATR * a)
    risk = abs(b["c"] - stop)
    sgn = 1 if long_ else -1
    end = "%02d:%02d" % divmod(int(b["time"][:2]) * 60 + int(b["time"][3:]) + 5, 60)
    c.update({"entry": round(b["c"], 2), "stop": round(stop, 2), "target1": round(b["c"] + sgn * 1.5 * risk, 2),
              "target2": round(b["c"] + sgn * 3 * risk, 2), "risk": round(risk, 2), "rr1": 1.5, "rr2": 3.0,
              "t1_from": "1.5R", "signal_time": "%s %s" % (b["date"], end), "daily_atr": round(a, 2)})
    return True


def build(args, notes, cands=None):
    """The brief. cands: a candidate list from funnel.py (setups on the catalyst's side); by default every setup
    in trader/orderblocks plus the macro engine's confirmations."""
    now = ih.now_ist().replace(second=0, microsecond=0)
    data = Data(args, notes, now)
    if cands is None:
        cands, macro = candidates(args.base, now)
    else:
        macro = load(os.path.join(args.base, "macro", "latest.json"))
    orders = load(os.path.join(args.base, "orders", "latest.json"))
    reg = load(os.path.join(args.base, "regime", "latest.json"))        # regime.py (trader-scanner): warn only
    if reg and (reg.get("checked") or "")[:10] < (now - _dt.timedelta(days=4)).strftime("%Y-%m-%d"):
        notes.append("the regime file is from %s: run trader-scanner/regime.py" % reg.get("checked"))
    fno, ban = {}, set()
    try:
        fno = im.fno_lots(data.client)
        ban = set((im.fno_ban(data.client) or {}).get("symbols") or [])
    except ih.FeedError as e:
        notes.append("F&O list: %s" % str(e)[:120])
    items = india_signals.load_all(args.base)
    active, caution, waiting = [], [], []
    for c in macro.get("caution") or []:
        if not c.get("gates_1_2"):           # an unconfirmed headline is not a headwind
            continue
        caution.append({"symbol": c["symbol"], "kind": "macro headwind", "why": c["why"]})
    for f in orders.get("watch") or []:
        caution.append({"symbol": f["symbol"], "kind": f["flag"], "why": (f.get("why") or f.get("title") or "")[:160],
                        "link": f.get("link")})
    for c in cands:
        sym = c["symbol"]
        if c["status"] == "blocked":
            caution.append({"symbol": sym, "kind": "retail trap guard", "why": "%s %s %s: %s" % (
                c["tf"], c["side"], c["origin"], "; ".join(g.replace("blocked: ", "") for g in c["guards"]))})
            continue
        if c["status"] == "expired":            # stop or target already traded after the signal
            notes.append("%s %s %s: %s" % (sym, c["tf"], c["side"], "; ".join(c.get("why_not") or [])[-120:]))
            continue
        if c["status"] == "waiting":
            waiting.append({"symbol": sym, "tf": c["tf"], "side": c["side"], "zone": c.get("zone"),
                            "why": "; ".join(c.get("why_not") or [])})
            continue
        if c.get("entry") is None and not macro_levels(data, c):
            notes.append("%s: no bars for the macro entry" % sym)
            continue
        if c["origin"].startswith("macro"):             # the same anti-trap windows as order-block entries
            g = anti_trap(c["signal_time"][11:16], args.no_entries_after)
            if g:
                caution.append({"symbol": sym, "kind": "retail trap guard",
                                "why": "%s %s: %s" % (c["side"], c["origin"], g)})
                continue
        sig = india_signals.signals_for(sym, now, 3, items=items)
        flags = india_signals.risk_flags(sig, now) + regime_flags(reg, c["side"])
        lot = fno.get(sym)
        banned = sym in ban
        side = c["side"]
        swing_ok = side == "long" and c["mode"] == "swing"
        bearish = c.get("daily_trend") == "bearish"
        macro_fno = im.TAG_FNO_SHORT in ((c.get("macro") or {}).get("tags") or [])
        tags = im.route(side, swing=swing_ok, fno_listed=bool(lot), fno_banned=banned,
                        multi_day_bearish=bearish and (c["mode"] == "swing" or macro_fno))
        if side == "long" and lot and not banned and c["mode"] == "swing":
            tags.append("[SECONDARY F&O]")
        entry, stop, t1 = c["entry"], c["stop"], c["target1"]
        if c["tf"] == "1d":
            intra = {"error": "a daily-candle setup: no intraday replay (the swing replay covers it)"}
        else:
            intra = intraday_tm(data, c, side, entry, stop, t1, args.sims)
        swing = None
        etype = None
        for it in (orders.get("items") or []):
            if it.get("symbol") == sym and it.get("event_type"):
                etype = it["event_type"]
                break
        if side == "long" or im.TAG_FNO_SHORT in tags:
            swing = swing_tm(data, sym, side, entry, stop, t1, args.sims, etype)
        win = exit_window(intra, swing if (side == "long" or im.TAG_FNO_SHORT in tags) else None)
        main_stats = swing if (swing_ok and swing and not swing.get("error")) else intra
        exp = (main_stats or {}).get("expectancy_R")
        edge = exp is not None and exp > 0
        row = {"symbol": sym, "side": side, "label": "[EQUITY LONG]" if side == "long" else "[EQUITY SHORT]",
               "tags": tags, "tf": c["tf"], "mode": c["mode"], "origin": c["origin"],
               "signal_time": c["signal_time"], "zone": c.get("zone"), "mean_threshold": c.get("mean_threshold"),
               "entry": entry, "stop": stop, "target1": t1, "target2": c.get("target2"), "rr1": c.get("rr1"),
               "confirmation": c.get("confirmation"), "vwap": c.get("vwap"), "vwap_kind": c.get("vwap_kind"),
               "oi": c.get("oi"), "daily_trend": c.get("daily_trend"),
               "macro": ({k: c["macro"][k] for k in ("label", "direction", "gate1", "gate2", "gate3")}
                         if c.get("macro") else None),
               "fno": {"lot": lot, "banned": banned} if lot else None,
               "intraday": intra, "swing": swing, "exit_window": win,
               "edge": edge, "edge_label": None if edge else "no edge in the Time Machine",
               "flags": flags}
        if side == "long" and c["mode"] == "swing":
            row["exit_by"] = next_sessions(c["signal_time"][:10], SWING_DAYS)[-1]
        if banned:
            caution.append({"symbol": sym, "kind": "F&O ban", "why": "in the F&O ban period: no new futures positions"})
        for f in flags:
            if f["flag"].startswith(("results due", "surveillance", "price band", "order loss", "execution lag",
                                     "debarment", "auditor")):
                caution.append({"symbol": sym, "kind": f["flag"], "why": f["why"][:160], "link": f.get("link")})
        active.append(row)
    active.sort(key=lambda r: (not r["edge"], -((r["swing"] or {}).get("expectancy_R") or 0
                                               if r["mode"] == "swing" else (r["intraday"] or {}).get("expectancy_R") or 0)))
    seen, uniq = set(), []
    for c in caution:
        k = (c["symbol"], c["kind"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return {"created_by": "trader-timemachine brief", "checked": now.strftime("%Y-%m-%d %H:%M"), "sims": args.sims,
            "regime": {k: reg.get(k) for k in ("regime", "score", "of", "volatility", "size_hint", "advice",
                                                "checked")} if reg else None,
            "active": active, "caution": uniq, "waiting": waiting, "notes": notes,
            "note": "History, not a forecast. Every figure is after costs (india_costs.py). "
                    "Suggestions only: nothing here places an order."}


# ---------------------------------------------------------------- output

def _r(x):
    return "n/a" if x is None else "%+.2fR" % x


def stat_line(st, kind):
    if not st:
        return "%s: not simulated" % kind
    if st.get("error"):
        return "%s: %s" % (kind, st["error"])
    mae = st.get("mae_R") or {}
    if kind == "intraday":
        return ("intraday TM (%d replays, %d sessions): win %.0f%%, target %.0f%% / stop %.0f%% / open at 15:15 %.0f%%, "
                "expectancy %s, R:R %.2f, MAE median %.2fR worst-5%% %.2fR" % (
                    st["replays"], st["sessions"], st["win_pct"], st["p_target"], st["p_stop"], st["p_open_at_flat"],
                    _r(st["expectancy_R"]), st["rr"], mae.get("median", 0), mae.get("p95", 0)))
    return ("swing TM (%d windows x %d draws): win %.0f%%, expectancy %s (%+.2f%% net), R:R %s, MAE median %.2fR "
            "worst-5%% %.2fR, gaps through the stop %.1f%%, 5-95%% %+.1f%% to %+.1f%%" % (
                st["windows"], st["sims"], st["win_pct"], _r(st["expectancy_R"]), st["mean_net_pct"],
                st.get("rr") or "n/a", mae.get("median", 0), mae.get("p95", 0), st["gap_through_stop_pct"],
                st["p5_pct"], st["p95_pct"]))


def render(doc):
    out = ["Trade brief %s IST (Time Machine: %d paths per trade)" % (doc["checked"], doc["sims"])]
    rg = doc.get("regime")
    out.append("Regime: %s" % ("%s (score %+d of %d), volatility %s, size %s: %s" % (
        rg["regime"].upper(), rg["score"], rg["of"], rg["volatility"], rg["size_hint"], rg["advice"])
        if rg else "unknown (run trader-scanner/regime.py)"))
    out += ["", "(a) ACTIVE SETUPS: %d" % len(doc["active"])]
    for n, r in enumerate(doc["active"], 1):
        out.append("%d. %s %s %s  %s" % (n, r["label"], r["symbol"], " ".join(r["tags"]),
                                         "" if r["edge"] else "<- " + r["edge_label"]))
        if r["zone"]:
            out.append("   order block zone %.2f-%.2f (%s), 50%% mean-threshold entry %.2f" % (
                r["zone"][0], r["zone"][1], r["tf"], r["mean_threshold"]))
        out.append("   entry %.2f at %s (%s); stop %.2f; T1 %.2f (%sR); T2 %s" % (
            r["entry"], r["signal_time"], r["confirmation"] or "-", r["stop"], r["target1"], r["rr1"],
            "%.2f" % r["target2"] if r["target2"] else "-"))
        if r["macro"]:
            out.append("   macro: %s %s; gate1 %s; gate2 %s" % (r["macro"]["label"], r["macro"]["direction"],
                                                               r["macro"]["gate1"][:60], r["macro"]["gate2"][:50]))
        out.append("   " + stat_line(r["intraday"], "intraday"))
        if r["swing"]:
            out.append("   " + stat_line(r["swing"], "swing"))
            if r["swing"].get("event_pool"):
                out.append("   " + stat_line(r["swing"]["event_pool"], "swing").replace("swing TM", "event-pool TM"))
        if r["exit_window"]:
            out.append("   optimal exit window: %s (%s); %s" % (
                r["exit_window"]["best"], _r(r["exit_window"]["avg_R"]),
                ", ".join("%s %s" % (o["exit"], _r(o["avg_R"])) for o in r["exit_window"]["options"])))
        if r["oi"]:
            out.append("   F&O OI: %s (%s)" % (r["oi"]["buildup"], "agrees" if r["oi"]["agrees"] else "disagrees"))
        for f in r["flags"]:
            out.append("   ! %s: %s" % (f["flag"], f["why"][:100]))
    if not doc["active"]:
        out.append("   none right now")
    out += ["", "(b) [CAUTION / DO NOT ENTER]: %d" % len(doc["caution"])]
    for c in doc["caution"]:
        out.append("   %-11s %-26s %s" % (c["symbol"], c["kind"], c["why"][:100]))
    if doc["waiting"]:
        out += ["", "Waiting for confirmation: %d" % len(doc["waiting"])]
        for w in doc["waiting"][:15]:
            out.append("   %-11s %-4s %-5s %s" % (w["symbol"], w["tf"], w["side"], w["why"][:100]))
    for n in doc["notes"]:
        out.append("   note: %s" % n)
    out.append(doc["note"])
    return "\n".join(out) + "\n"


def markdown(doc):
    lines = ["# Trade brief %s IST" % doc["checked"], "", "## Active setups", ""]
    for n, r in enumerate(doc["active"], 1):
        lines.append("%d. **%s %s** %s%s" % (n, r["label"], r["symbol"], " ".join(r["tags"]),
                                              "" if r["edge"] else " (no edge in the Time Machine)"))
        lines.append("   - entry %.2f, stop %.2f, T1 %.2f, T2 %s" % (r["entry"], r["stop"], r["target1"], r["target2"]))
        lines.append("   - " + stat_line(r["intraday"], "intraday"))
        if r["swing"]:
            lines.append("   - " + stat_line(r["swing"], "swing"))
        if r["exit_window"]:
            lines.append("   - optimal exit window: %s" % r["exit_window"]["best"])
    lines += ["", "## Caution / do not enter", ""]
    for c in doc["caution"]:
        lines.append("- **%s** %s: %s" % (c["symbol"], c["kind"], c["why"]))
    lines += ["", doc["note"], ""]
    return "\n".join(lines)


class PlanError(Exception):
    pass


def to_plan(r, now, futures=False):
    """An active setup -> trader/plan.json (the shape /trader-exit, budget, risk and alerts read).
    futures=True: the [SECONDARY F&O SWING SHORT OPTION] leg, an overnight stock-futures short in whole lots."""
    if futures:
        if im.TAG_FNO_SHORT not in r["tags"] or not (r.get("fno") or {}).get("lot"):
            raise PlanError("setup %s has no F&O swing-short leg (it needs an F&O stock out of the ban period with a "
                            "multi-day bearish setup)" % r["symbol"])
    swing = (r["mode"] == "swing" and r["side"] == "long") or futures
    risk = abs(r["entry"] - r["stop"])
    hhmm = r["signal_time"][11:16]
    plan = {"created_by": "trader-timemachine brief.py", "symbol": r["symbol"],
            "side": "buy" if r["side"] == "long" else "sell", "date": r["signal_time"][:10],
            "setup": "order_block" if r["zone"] else "macro", "entry": r["entry"], "entry_time": hhmm,
            "interval": "5m", "stop": r["stop"], "stop_rule": "order block far edge" if r["zone"] else "event bar",
            "target": r["target1"], "target_r": round(abs(r["target1"] - r["entry"]) / risk, 2) if risk else None,
            "risk_per_share": round(risk, 2),
            "reward_risk": round(abs(r["target1"] - r["entry"]) / risk, 2) if risk else None,
            "flat_by": None if swing else "15:15", "mode": "swing" if swing else "day",
            "exit_by": (r.get("exit_by") or next_sessions(r["signal_time"][:10], SWING_DAYS)[-1]) if swing else None,
            "hold_sessions": SWING_DAYS if swing else 0,
            "gap_risk_pct": abs((r["swing"] or {}).get("p5_pct") or 0) or None if swing else None,
            "tags": r["tags"], "source": "%s %s" % (r["origin"], r["signal_time"]),
            "blocked_windows": [list(w) for w in BLOCKED_WINDOWS] if not swing else [],
            "created_at": now.strftime("%Y-%m-%d %H:%M IST"),
            "note": "From the trade brief. History, not a forecast; an overnight gap can open past any stop."}
    if r["side"] == "short":
        plan["short"] = True
    if futures:
        plan.update({"instrument": "futures", "lot_size": int(r["fno"]["lot"])})
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description="Universal Time Machine & trade brief (trader-timemachine).")
    ap.add_argument("command", nargs="?", default="run", choices=("run", "plan"))
    ap.add_argument("n", nargs="?", type=int, help="plan: the setup number from the brief")
    ap.add_argument("--base", default="trader")
    ap.add_argument("--sims", type=int, default=SIMS)
    ap.add_argument("--no-entries-after", default="14:50", help="last intraday entry time (14:50)")
    ap.add_argument("--months", type=int, default=24, help="daily history for the swing replay (24 months)")
    ap.add_argument("--events-db", default=ev.DB_PATH)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--write", action="store_true", help="plan: write trader/plan.json")
    ap.add_argument("--futures", action="store_true", help="plan: the F&O swing-short leg (stock futures, whole lots)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-10-23 13:05"')
    ap.add_argument("--offline", metavar="DIR", help="read saved charts from DIR")
    a = ap.parse_args(argv)
    ih.pin_clock(a.now)
    if a.command == "plan":
        doc = load(os.path.join(a.out, "latest.json"))
        rows = doc.get("active") or []
        if not a.n or not 1 <= a.n <= len(rows):
            print("brief.py plan: pick a setup 1..%d from %s" % (len(rows), os.path.join(a.out, "latest.json")),
                  file=sys.stderr)
            return 2
        try:
            plan = to_plan(rows[a.n - 1], ih.now_ist(), futures=a.futures)
        except PlanError as e:
            print("brief.py plan: %s" % e, file=sys.stderr)
            return 2
        print(json.dumps(plan, indent=2))
        if a.write:
            ih.write_json(PLAN, plan)
            print("Wrote %s. Next: /trader-exit, /trader-budget, /trader-risk. Nothing was placed." % PLAN)
        return 0
    notes = []
    doc = build(a, notes)
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        sys.stdout.write(render(doc))
    if not a.dry_run:
        ih.write_json(os.path.join(a.out, "latest.json"), doc)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "latest.md"), "w", encoding="utf-8") as fh:
            fh.write(markdown(doc))
    return 0 if doc["active"] else 1


if __name__ == "__main__":
    sys.exit(main())
