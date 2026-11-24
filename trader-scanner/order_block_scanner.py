#!/usr/bin/env python3
"""Multi-Timeframe Order Block & Anti-Trap Entry Scanner (trader-scanner): unmitigated bullish and
bearish order blocks on 5-min, 15-min, 1-hour and daily candles, and an entry only when the price comes
back to one AND confirms. Plain arithmetic on candles, no language model.

    python3 order_block_scanner.py                          # symbols from the orders tracker, the macro
                                                            # engine and the watchlist -> trader/orderblocks/
    python3 order_block_scanner.py --symbols KEC,RVNL,HAL   # these symbols
    python3 order_block_scanner.py --tf 15m,1d --all        # some timeframes; waiting / blocked ones too
    python3 order_block_scanner.py --offline DIR --now "2026-10-23 13:05" --dry-run

What counts as an order block (OB):
  impulse   within 3 candles of the OB candle, price travels >= 2x ATR(14) of that timeframe
  BOS       the impulse closes beyond the last swing high (bullish) / swing low (bearish) (2-bar fractals)
  FVG       the impulse leaves a 3-candle fair value gap (candle 1's high below candle 3's low, or the
            mirror for bearish)
  the OB    the last opposite-coloured candle before the impulse; zone = its high to low, mean threshold
            = 50 % of it. Unmitigated until price first comes back into the zone; invalid on a close
            beyond its far edge.
An entry fires only on the first return to an unmitigated OB, and only with confirmation:
  1. a liquidity sweep and reclaim (a wick beyond the zone / the swing level that closes back inside,
     on >= 1.8x the 20-bar average volume) OR an absorption candle (>= 1.8x volume, a narrow body or a
     long wick, the close inside the zone and in the top 30 % of the candle for longs, bottom 30 % for
     shorts);
  2. VWAP: intraday (5m, 15m) the close is on the right side of the session VWAP; swing (60m, 1d) of the
     VWAP anchored on the event date (order win, results, macro shock: from trader/orders,
     trader/catalysts, trader/macro), or on the OB's own date when there is no event;
  3. F&O open interest from NSE's daily F&O bhavcopy, a secondary note only (long / short build-up).
Anti-trap guards (India time): no intraday entries 09:15-09:30 (opening noise) or 11:45-13:30 (midday
fakeouts) or after the config's no_entries_after (14:50); no entry when the price is already more than
1.5x the 14-day ATR from the previous close (intraday) or from the anchored VWAP (swing).

Output: trader/orderblocks/latest.json: {"setups": [...], "notes"}. Each setup has the zone, the 50 %
entry, the confirmation, a stop beyond the far edge (+0.1 ATR), targets (the next opposing swing, or
1.5R, and 3R), the guards it passed or failed, and the daily trend (for the F&O swing-short rule).
Next: trader-timemachine/brief.py runs every setup through the Time Machine.

Volume is volume: a big candle does not prove an institution traded it. Nothing here places an order.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_bars as ib  # noqa: E402
import india_http as ih  # noqa: E402
import india_market as im  # noqa: E402

TIMEFRAMES = ("5m", "15m", "60m", "1d")
INTRADAY = ("5m", "15m")
IMPULSE_ATR = 2.0
IMPULSE_BARS = 3
VOL_CONFIRM = 1.8
CLOSE_SHARE = 0.30
EXTENSION_ATR = 1.5
STOP_BUFFER_ATR = 0.1
BLOCKED = (("09:15", "09:30", "opening noise (09:15-09:30)"), ("11:45", "13:30", "midday fakeouts (11:45-13:30)"))
NO_ENTRIES_AFTER = "14:50"
FRESH_BARS = 3          # a tap older than this many bars is no longer a live entry
OUT_DIR = os.path.join("trader", "orderblocks")
CONFIG = os.path.join("trader", "config.json")


# ---------------------------------------------------------------- structure

def swings(bars, k=2):
    """Fractal swing highs and lows: (indices of highs, indices of lows). A swing at i is known at i+k."""
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i]["h"] == max(b["h"] for b in win) and all(bars[i]["h"] > b["h"] for b in win if b is not bars[i]):
            hi.append(i)
        if bars[i]["l"] == min(b["l"] for b in win) and all(bars[i]["l"] < b["l"] for b in win if b is not bars[i]):
            lo.append(i)
    return hi, lo


def _last_swing(idx, before, k=2):
    known = [i for i in idx if i + k < before]
    return known[-1] if known else None


def find_order_blocks(bars, atr_n=14, impulse_atr=IMPULSE_ATR, max_bars=IMPULSE_BARS):
    """Every order block in the bars, oldest first, with its status now (fresh, tapped, invalid)."""
    hi, lo = swings(bars)
    atrs = ib.atr_series(bars, atr_n)
    out = []
    for i in range(atr_n, len(bars) - 3):
        a = atrs[i]
        if not a:
            continue
        b = bars[i]
        for side in ("bull", "bear"):
            if (side == "bull") != (b["c"] < b["o"]):       # the OB is the last opposite-coloured candle
                continue
            nxt = bars[i + 1]
            if (side == "bull") != (nxt["c"] > nxt["o"]):    # the impulse starts on the next candle
                continue
            imp = bars[i + 1:i + 1 + max_bars]
            travel = (max(x["h"] for x in imp) - b["l"]) if side == "bull" else (b["h"] - min(x["l"] for x in imp))
            if travel < impulse_atr * a:
                continue
            sw = _last_swing(hi if side == "bull" else lo, i + 1)
            if sw is None:
                continue
            level = bars[sw]["h"] if side == "bull" else bars[sw]["l"]
            bos = [j for j in range(i + 1, min(len(bars), i + 1 + max_bars))
                   if (bars[j]["c"] > level if side == "bull" else bars[j]["c"] < level)]
            if not bos:
                continue
            fvg = None
            for j in range(i + 1, min(len(bars) - 1, i + max_bars + 1)):
                if j - 1 < 0:
                    continue
                if side == "bull" and bars[j - 1]["h"] < bars[j + 1]["l"]:
                    fvg = (bars[j - 1]["h"], bars[j + 1]["l"])
                    break
                if side == "bear" and bars[j - 1]["l"] > bars[j + 1]["h"]:
                    fvg = (bars[j + 1]["h"], bars[j - 1]["l"])
                    break
            if not fvg:
                continue
            ob = {"side": side, "index": i, "date": b["date"], "time": b["time"], "low": b["l"], "high": b["h"],
                  "mid": round((b["l"] + b["h"]) / 2, 2), "atr": a, "impulse_atr": round(travel / a, 2),
                  "bos_level": level, "bos_index": bos[0], "fvg": [round(fvg[0], 2), round(fvg[1], 2)],
                  "tapped_at": None, "invalid_at": None}
            start = i + 1 + max_bars
            for j in range(start, len(bars)):
                x = bars[j]
                if ob["tapped_at"] is None and (x["l"] <= ob["high"] if side == "bull" else x["h"] >= ob["low"]):
                    ob["tapped_at"] = j
                if (x["c"] < ob["low"]) if side == "bull" else (x["c"] > ob["high"]):
                    ob["invalid_at"] = j
                    break
            ob["status"] = "invalid" if ob["invalid_at"] is not None else "tapped" if ob["tapped_at"] is not None \
                else "fresh"
            out.append(ob)
    return out


# ---------------------------------------------------------------- confirmation and guards

def candle_share(b):
    """Where the close sits in the candle: 0 = at the low, 1 = at the high."""
    rng = b["h"] - b["l"]
    return (b["c"] - b["l"]) / rng if rng > 0 else 0.5


def confirmation(bars, ob, j, swing_level=None, vol_n=20):
    """('sweep and reclaim' | 'absorption' | None, volume ratio) for bar j against the OB."""
    b = bars[j]
    vr = ib.vol_ratio(bars, j, vol_n)
    if vr is None or vr < VOL_CONFIRM:
        return None, vr
    bull = ob["side"] == "bull"
    far = ob["low"] if bull else ob["high"]
    lvl = [far] + ([swing_level] if swing_level is not None else [])
    if bull and any(b["l"] < x < b["c"] for x in lvl) and b["c"] >= ob["low"]:
        return "sweep and reclaim", vr
    if not bull and any(b["c"] < x < b["h"] for x in lvl) and b["c"] <= ob["high"]:
        return "sweep and reclaim", vr
    rng = b["h"] - b["l"]
    body = abs(b["c"] - b["o"])
    narrow = rng > 0 and (body <= 0.4 * rng or rng <= 0.6 * ob["atr"])
    inside = ob["low"] <= b["c"] <= ob["high"]
    share = candle_share(b)
    if narrow and inside and (share >= 1 - CLOSE_SHARE if bull else share <= CLOSE_SHARE):
        return "absorption", vr
    return None, vr


def bar_close_time(b, tf):
    hh, mm = map(int, b["time"].split(":"))
    m = hh * 60 + mm + ib.STEP[tf]
    return "%02d:%02d" % (m // 60, m % 60)


def time_guard(close_hhmm, no_after=NO_ENTRIES_AFTER):
    for a, z, why in BLOCKED:
        if a <= close_hhmm <= z:
            return "blocked: " + why
    if close_hhmm > no_after:
        return "blocked: after no_entries_after %s" % no_after
    return None


def daily_trend(daily):
    """'bearish' when the last close is under the 20-day average with lower swing highs, 'bullish' for the
    mirror, else 'mixed'. The F&O swing-short tag needs 'bearish'."""
    if len(daily) < 25:
        return "unknown"
    closes = [b["c"] for b in daily]
    sma = sum(closes[-20:]) / 20
    hi, lo = swings(daily)
    hs = [daily[i]["h"] for i in hi[-3:]]
    ls = [daily[i]["l"] for i in lo[-3:]]
    if closes[-1] < sma and len(hs) >= 2 and all(a > b for a, b in zip(hs, hs[1:])):
        return "bearish"
    if closes[-1] > sma and len(ls) >= 2 and all(a < b for a, b in zip(ls, ls[1:])):
        return "bullish"
    return "mixed"


def daily_from_intraday(bars):
    """5-min bars -> daily bars (when NSE's daily server is not at hand)."""
    out = {}
    for b in bars:
        d = out.get(b["date"])
        if d is None:
            out[b["date"]] = dict(b, time="")
        else:
            d["h"], d["l"], d["c"], d["v"] = max(d["h"], b["h"]), min(d["l"], b["l"]), b["c"], d["v"] + b["v"]
    return [out[k] for k in sorted(out)]


def plan_levels(bars, ob, entry, j, hi_idx, lo_idx):
    """Stop beyond the far edge (+0.1 ATR); T1 = the next opposing swing if at least 1R away, else 1.5R;
    T2 = 3R."""
    bull = ob["side"] == "bull"
    a = ib.atr(bars, 14, j) or ob["atr"]
    stop = (ob["low"] - STOP_BUFFER_ATR * a) if bull else (ob["high"] + STOP_BUFFER_ATR * a)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    swing = None
    pool = [bars[i]["h"] for i in hi_idx if i < j and bars[i]["h"] > entry] if bull else \
        [bars[i]["l"] for i in lo_idx if i < j and bars[i]["l"] < entry]
    if pool:
        swing = min(pool) if bull else max(pool)
    sgn = 1 if bull else -1
    t1 = swing if (swing is not None and abs(swing - entry) >= risk) else entry + sgn * 1.5 * risk
    t2 = entry + sgn * 3 * risk
    return {"stop": round(stop, 2), "target1": round(t1, 2), "target2": round(t2, 2), "risk": round(risk, 2),
            "t1_from": "the next opposing swing" if (swing is not None and abs(swing - entry) >= risk) else "1.5R",
            "rr1": round(abs(t1 - entry) / risk, 2), "rr2": 3.0}


def evaluate(symbol, tf, bars, now_hhmm=None, daily=None, anchor=None, oi=None, fresh_bars=FRESH_BARS,
             no_after=NO_ENTRIES_AFTER):
    """Order blocks tapped in the last `fresh_bars` bars -> setups with confirmation, guards and levels."""
    if len(bars) < 30:
        return []
    obs = find_order_blocks(bars)
    hi, lo = swings(bars)
    last = len(bars) - 1
    setups = []
    daily = daily or []
    d_atr = ib.atr(daily, 14) if daily else None
    trend = daily_trend(daily) if daily else "unknown"
    for ob in obs:
        t = ob["tapped_at"]
        if t is None or t < last - fresh_bars + 1 or (ob["invalid_at"] is not None and ob["invalid_at"] <= t):
            continue
        swing_level = None
        idx = lo if ob["side"] == "bull" else hi
        prior = [i for i in idx if i + 2 < t]
        if prior:
            swing_level = bars[prior[-1]]["l"] if ob["side"] == "bull" else bars[prior[-1]]["h"]
        how, vr, j = None, None, t
        for jj in range(t, min(last, t + 1) + 1):
            how, vr = confirmation(bars, ob, jj, swing_level)
            j = jj
            if how:
                break
        b = bars[j]
        bull = ob["side"] == "bull"
        guards = []
        if ob["invalid_at"] is not None and ob["invalid_at"] <= j:
            continue
        intraday = tf in INTRADAY
        if intraday:
            v = ib.session_vwap(bars, j)
            vwap_kind = "session VWAP"
        else:
            v = ib.anchored_vwap(bars, anchor or ob["date"], j)
            vwap_kind = "VWAP anchored on %s%s" % (anchor or ob["date"], "" if anchor else " (the OB's date: no event)")
        vwap_ok = v is not None and (b["c"] >= v if bull else b["c"] <= v)
        if intraday:
            g = time_guard(bar_close_time(b, tf), no_after)
            if g:
                guards.append(g)
        if d_atr:
            ref_bar = [d for d in daily if d["date"] < b["date"]]
            ref = (ref_bar[-1]["c"] if ref_bar else None) if intraday else v
            if ref is not None and abs(b["c"] - ref) > EXTENSION_ATR * d_atr:
                guards.append("blocked: price %.2f is %.1fx the 14-day ATR from the %s (limit %.1fx)"
                              % (b["c"], abs(b["c"] - ref) / d_atr, "previous close" if intraday else vwap_kind,
                                 EXTENSION_ATR))
        lv = plan_levels(bars, ob, b["c"], j, hi, lo)
        if not lv:
            continue
        status = "entry" if (how and vwap_ok and not guards) else "blocked" if guards else "waiting"
        why = []
        if not how:
            why.append("no sweep-and-reclaim or absorption candle yet (volume %sx, need %.1fx)"
                       % (vr if vr is not None else "?", VOL_CONFIRM))
        if not vwap_ok:
            why.append("close %s the %s %s" % ("under" if bull else "over", vwap_kind,
                                               "%.2f" % v if v is not None else "(none)"))
        buildup = im.oi_buildup(oi) if oi else None
        setups.append({
            "symbol": symbol, "tf": tf, "mode": "intraday" if intraday else "swing",
            "side": "long" if bull else "short", "status": status,
            "zone": [round(ob["low"], 2), round(ob["high"], 2)], "mean_threshold": ob["mid"],
            "ob_time": "%s %s" % (ob["date"], ob["time"]), "impulse_atr": ob["impulse_atr"], "fvg": ob["fvg"],
            "bos_level": round(ob["bos_level"], 2),
            "signal_time": "%s %s" % (b["date"], bar_close_time(b, tf) if b["time"] else ""),
            "entry": round(b["c"], 2), "confirmation": how, "volume_ratio": vr,
            "vwap": round(v, 2) if v is not None else None, "vwap_kind": vwap_kind, "vwap_ok": vwap_ok,
            "anchor": anchor, "oi": ({"buildup": buildup, "oi_change_pct": oi.get("oi_change_pct"), "day": oi.get("day"),
                                      "agrees": (buildup == "long build-up") if bull else (buildup == "short build-up")}
                                     if buildup else None),
            "guards": guards, "why_not": why, "daily_trend": trend,
            "daily_atr": round(d_atr, 2) if d_atr else None, **lv,
        })
    return setups


# ---------------------------------------------------------------- the universe and the events

def _items(path):
    doc = ih.load_json(path) or {}
    return doc.get("items") or []


def universe(args):
    syms = []
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        base = args.base
        for it in _items(os.path.join(base, "orders", "latest.json")):
            if it.get("symbol") and it.get("kind") in ("award", "cancel", "delay", "debar"):
                syms.append(it["symbol"].upper())
        macro = ih.load_json(os.path.join(base, "macro", "latest.json")) or {}
        for key in ("actionable_long", "actionable_short"):
            for row in macro.get(key) or []:
                syms.append(row["symbol"].upper())
        wl = ih.load_json(os.path.join(base, "watchlist.json")) or {}
        for h in wl.get("hits") or []:
            if h.get("symbol"):
                syms.append(str(h["symbol"]).upper())
    out = []
    for s in syms:
        s = s[:-3] if s.endswith(".NS") else s
        if s and s not in out:
            out.append(s)
    return out[:args.max_symbols]


def event_anchors(base):
    """{SYMBOL: newest event date} from the orders, catalysts and macro outputs."""
    out = {}
    for sub in ("orders", "catalysts"):
        for it in _items(os.path.join(base, sub, "latest.json")):
            sym, t = (it.get("symbol") or "").upper(), (it.get("time") or "")[:10]
            if sym and t and t > out.get(sym, ""):
                out[sym] = t
    macro = ih.load_json(os.path.join(base, "macro", "latest.json")) or {}
    for key in ("actionable_long", "actionable_short", "caution"):
        for row in macro.get(key) or []:
            t = (row.get("event_time") or "")[:10]
            if t and t > out.get(row["symbol"], ""):
                out[row["symbol"]] = t
    return out


def no_entries_after(path):
    cfg = ih.load_json(path) or {}
    return str(cfg.get("no_entries_after") or NO_ENTRIES_AFTER)


# ---------------------------------------------------------------- run

def scan(args, notes):
    now = ih.now_ist()
    now_utc = now - ib.IST
    syms = universe(args)
    if not syms:
        notes.append("no symbols: pass --symbols, or run govt_orders_tracker.py / global_macro_engine.py / "
                      "scan.py first")
        return []
    tfs = [t for t in args.tf.split(",") if t in TIMEFRAMES]
    anchors = event_anchors(args.base)
    oi = {}
    client = ih.Client(offline=args.offline)
    if not args.no_oi:
        try:
            oi, oi_day = im.fno_oi(client, now.date(), notes=notes)
        except Exception as e:  # OI is a secondary note: never fatal
            notes.append("F&O open interest: %s" % str(e)[:120])
    no_after = no_entries_after(args.config)
    setups = []
    for sym in syms:
        try:
            five = ib.parse(ib.fetch_text(sym, "5m", offline=args.offline), now_utc) if \
                any(t in ("5m", "15m") for t in tfs) else []
        except ib.BarsError as e:
            notes.append("%s 5m: %s" % (sym, e))
            five = []
        try:
            daily = ib.nse_daily(sym, 6, offline=args.offline)
            daily = [d for d in daily if d["date"] <= now.strftime("%Y-%m-%d")]
        except Exception as e:
            daily = daily_from_intraday(five)
            if not daily:
                notes.append("%s daily: %s" % (sym, str(e)[:120]))
        today = now.strftime("%Y-%m-%d")
        daily_closed = [d for d in daily if d["date"] < today]
        for tf in tfs:
            if tf == "5m":
                bars = five
            elif tf == "15m":
                bars = ib.resample(five, 15)
            elif tf == "60m":
                try:
                    bars = ib.parse(ib.fetch_text(sym, "60m", offline=args.offline), now_utc)
                except ib.BarsError as e:
                    notes.append("%s 60m: %s" % (sym, e))
                    continue
            else:
                bars = daily_closed
            if tf in INTRADAY or tf == "60m":
                clock = now.strftime("%H:%M")       # a resampled candle counts only once it has closed
                bars = [b for b in bars if b["date"] < today or (b["date"] == today and bar_close_time(b, tf) <= clock)]
            setups += evaluate(sym, tf, bars, daily=daily_closed, anchor=anchors.get(sym), oi=oi.get(sym),
                               no_after=no_after)
    order = {"entry": 0, "waiting": 1, "blocked": 2}
    setups.sort(key=lambda s: (order[s["status"]], s["symbol"], TIMEFRAMES.index(s["tf"])))
    return setups


def render(setups, notes, shown):
    out = ["Order block scan, %s IST: %d setup(s), %d entries" % (ih.now_ist().strftime("%Y-%m-%d %H:%M"), len(setups),
                                                                  sum(1 for s in setups if s["status"] == "entry"))]
    for s in shown:
        out.append("%s %-10s %-4s %-8s %-5s zone %.2f-%.2f  50%% %.2f  close %.2f  stop %.2f  T1 %.2f  T2 %.2f" % (
            {"entry": "*", "waiting": " ", "blocked": "x"}[s["status"]], s["symbol"], s["tf"], s["mode"], s["side"],
            s["zone"][0], s["zone"][1], s["mean_threshold"], s["entry"], s["stop"], s["target1"], s["target2"]))
        bits = [s["status"].upper(), s["signal_time"]]
        if s["confirmation"]:
            bits.append("%s on %sx volume" % (s["confirmation"], s["volume_ratio"]))
        bits.append("%s %s" % (s["vwap_kind"], "held" if s["vwap_ok"] else "not held"))
        if s["oi"]:
            bits.append("OI: %s" % s["oi"]["buildup"])
        out.append("      " + "; ".join(bits))
        for g in s["guards"] + s["why_not"]:
            out.append("      - " + g)
    if not shown:
        out.append("  no entries (use --all to see waiting and blocked setups)")
    for n in notes:
        out.append("  note: %s" % n)
    out.append("Next: trader-timemachine/brief.py simulates every setup. Volume is not proof of who traded.")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Multi-timeframe order block & anti-trap entry scanner (trader-scanner).")
    ap.add_argument("--symbols", help="comma-separated NSE symbols (default: orders, macro and watchlist outputs)")
    ap.add_argument("--tf", default=",".join(TIMEFRAMES), help="timeframes: 5m,15m,60m,1d")
    ap.add_argument("--all", action="store_true", help="also print waiting and blocked setups")
    ap.add_argument("--max-symbols", type=int, default=40)
    ap.add_argument("--base", default="trader", help="where the other tools' outputs are (trader)")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--no-oi", action="store_true", help="skip NSE's F&O bhavcopy")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--now", help='pin the clock, India time: "2026-10-23 13:05"')
    ap.add_argument("--offline", metavar="DIR", help="read saved charts from DIR (<SYM>.NS_5m.json, _60m, _1d)")
    a = ap.parse_args(argv)
    ih.pin_clock(a.now)
    notes = []
    setups = scan(a, notes)
    shown = setups if a.all else [s for s in setups if s["status"] == "entry"]
    doc = {"created_by": "trader-scanner order_block_scanner", "checked": ih.now_ist().strftime("%Y-%m-%d %H:%M"),
           "setups": setups, "notes": notes}
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        sys.stdout.write(render(setups, notes, shown))
    if not a.dry_run:
        ih.write_json(os.path.join(a.out, "latest.json"), doc)
    return 0 if any(s["status"] == "entry" for s in setups) else 1


if __name__ == "__main__":
    sys.exit(main())
