#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote TSLA --write             # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol TSLA --stop 358.32
    python3 alerts.py add --symbol TSLA --target 370.04
    python3 alerts.py add --at 15:15 --label "flat by close"
    python3 alerts.py add --symbol KO --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-19 15:15" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset tsla-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE opened today (rings once per trading day)
  time            the India (IST) clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  earlier if the NSE ever shortens a session (NSE has no
                  scheduled early closes, so today it changes nothing)

A rule with "date" is only watched on that India date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (for alerts and sizing checks, never for timing an order): NSE's official
CM Market Live MCP server, https://mcp.nseindia.in/cmmkt/mcp, tool
cm_get_stock_quote, one call per symbol over one connection (data refreshes
every minute), through nse_data.py with the mcp Python SDK. No language model
is involved. Symbols are NSE's own (RELIANCE, TCS); a .NS suffix is accepted.
A symbol NSE cannot serve (time out, failure, empty answer) goes to the backup
hook, nse_data.fetch_backup_data(): today the last quote cached in
trader/cache/quotes.csv, marked "source": "cache" and keeping its quote time.
  --offline DIR reads DIR/nse-<SYMBOL>.json (a cm_get_stock_quote answer) instead.

NSE calendar: the session is 9:15 a.m. to 3:30 p.m. India time (IST, UTC+5:30,
no daylight saving), and the 2026 equity-segment trading holidays as published
by the NSE (https://www.nseindia.com/resources/exchange-communication-holidays).
Outside those years the clock says the holiday list is unknown.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_data  # noqa: E402  (NSE's official MCP servers + the backup hook; this folder)
import india_calendar  # noqa: E402  (one NSE holiday list for every tool)
import india_signals  # noqa: E402  (what trader-news and trader-pelosi know about a symbol)

HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

NSE_HOLIDAYS = india_calendar.NSE_HOLIDAYS  # NSE trading holidays: built-in 2026 + NSE's API + trader/nse_holidays.json
NSE_EARLY_CLOSE = {}  # NSE has no scheduled early closes; add a date here if the exchange shortens a session
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(13, 0)


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time: UTC+5:30 all year, no daylight saving


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time (IST)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (India, IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in India time (IST), or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE)


def market_status(now_utc):
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": india_calendar.known(d.year),
          "open": False, "reason": reason, "early_close": NSE_EARLY_CLOSE.get(d.isoformat())}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            st["next_open"] = "%s 09:15" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def _offline_doc(folder, sym):
    for name in ("nse-%s.json" % sym, "nse-%s.json.gz" % sym):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            if name.endswith(".gz"):
                import gzip
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    text = fh.read()
            else:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            try:
                return json.loads(text)
            except ValueError as e:
                raise AlertError("%s is not valid JSON: %s" % (path, e))
    raise AlertError("no offline quote file %s" % os.path.join(folder, "nse-%s.json" % sym))


def get_quotes(symbols, offline=None, cache_dir=None):
    """{SYM: quote} and [errors]. Live: NSE's MCP server, with the backup hook for any symbol it
    cannot serve. Offline: DIR/nse-<SYM>.json files (no backup)."""
    syms = []
    for s in symbols:
        s = nse_data.nse_symbol(s)
        if s and s not in syms:
            syms.append(s)
    if not offline:
        quotes, errs = nse_data.fetch_quotes(syms, cache_dir=cache_dir)
        return quotes, ["%s: %s" % (s, e) for s, e in sorted(errs.items())]
    quotes, errors = {}, []
    for sym in syms:
        try:
            doc = _offline_doc(offline, sym)
            if isinstance(doc, dict) and doc.get("error"):
                raise AlertError("%s: %s" % (sym, doc["error"]))
            quotes[sym] = nse_data.parse_live_quote(doc, sym)
        except (AlertError, nse_data.NSEError) as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time", "catalyst"}
NO_QUOTE = {"opening_bell", "time", "catalyst"}
CATALYST_TYPES = {"catalyst"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if (t not in NO_QUOTE or t in CATALYST_TYPES) and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an India time (IST) like \"15:15\"" % rule.get("id"))
    for k in ("date", "until"):
        if rule.get(k) and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule[k])):
            raise AlertError("rule %s: %s must be YYYY-MM-DD" % (rule.get("id"), k))


def active(rule, today):
    """A rule with a "date" is watched on that India date only; with "until" too, from date to until."""
    if rule.get("until"):
        return str(rule.get("date") or "") <= today <= str(rule["until"])
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this India date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today, moved earlier on an early close if "before_close"."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        day = datetime.date(2000, 1, 1)
        at = (datetime.datetime.combine(day, at) - (datetime.datetime.combine(day, CLOSE)
                                                     - datetime.datetime.combine(day, EARLY))).time()
    return at


def evaluate(rules, quotes, status, state, now_utc, signals=None):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.
    signals: {SYMBOL: [catalyst / smart-money items]} for "catalyst" rules (india_signals)."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "catalyst":
            sym = nse_data.nse_symbol(r["symbol"])
            seen = state.setdefault("seen_items", {}).setdefault(rid, [])
            fresh = [i for i in (signals or {}).get(sym, []) if i["id"] not in seen]
            for i in fresh[:5]:
                what = "filing" if i["kind"] == "catalyst" else "smart money"
                rings.append({"id": rid, "symbol": sym, "message": "%s new %s (%s): %s %s" % (
                    sym, what, i["tag"] or i["category"] or i["source"], i["title"][:150], i["link"])})
            if fresh:
                seen.extend(i["id"] for i in fresh)
                del seen[:-300]
                rung[rid] = stamp
            continue
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE is open (%s IST)%s."
                              % (status["now_ist"], (", early close at %s today, " % EARLY.strftime("%H:%M")
                                                          + status["early_close"]) if status.get("early_close") else "")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                                 local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if r.get("advice"):
                    msg += " " + r["advice"]
                elif sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym) or quotes.get(nse_data.nse_symbol(sym))
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                if r.get("note", "").startswith("plan stop"):
                    label = "stop"
                elif r.get("note", "").startswith("plan target"):
                    label = "target"
                rings.append({"id": rid, "symbol": sym, "message": "%s %s ₹%.2f: %s your %s ₹%s%s."
                              % (sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to ₹%.2f (from ₹%.2f)."
                              % (sym, move, p, float(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    out = []
    short = str(plan.get("side") or "buy").lower() in ("sell", "short")
    kinds = ((("stop", "above", "plan stop (short)"), ("target", "below", "plan target (short)")) if short
             else (("stop", "stop", "plan stop"), ("target", "target", "plan target")))
    for key, typ, word in kinds:
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    swing = plan.get("mode") == "swing"
    if swing:
        end = str(plan.get("exit_by") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            raise AlertError("the swing plan has no exit_by date (run trader-exit --mode swing)")
        out.append({"id": "%s-gap-check" % sym.lower(), "symbol": sym, "type": "time", "at": "09:20",
                    "label": "overnight gap check: is %s still above the stop?" % sym, "after_date": True,
                    "advice": "Compare the open with your stop ₹%s and re-place the stop for today: day orders "
                              "expired at yesterday's close." % _n(plan["stop"])})
        out.append({"id": "%s-exit-by" % sym.lower(), "symbol": sym, "type": "time", "at": cfg_flat_by or "15:15",
                    "label": "swing exit day (%s)" % end, "before_close": True, "on": end})
    else:
        flat = plan.get("flat_by") or cfg_flat_by or "15:15"
        if _hhmm(flat) is None:
            raise AlertError("the plan's flat_by %r is not a time like 15:15" % flat)
        out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                    "label": "flat by close", "before_close": True})
    out.append({"id": "%s-catalysts" % sym.lower(), "symbol": sym, "type": "catalyst",
                "note": "new filings and smart-money items (trader-news / trader-pelosi)"})
    if plan.get("date"):
        for r in out:
            on = r.pop("on", None)
            after = r.pop("after_date", False)
            if on:
                r["date"] = on
            elif swing:
                day = datetime.date.fromisoformat(str(plan["date"]))
                r["date"] = (day + datetime.timedelta(days=1)).isoformat() if after else day.isoformat()
                r["until"] = str(plan["exit_by"])
            else:
                r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("ticker") or p.get("symbol") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("India %s IST (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                                  "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s%s." % (st["opens"], st["closes"],
                                                    (" (early close: %s)" % st["early_close"]) if st.get("early_close") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, india_calendar.years())))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([nse_data.nse_symbol(s) for s in a.symbols] +
                   [nse_data.nse_symbol(s) for s in (holdings(a) if a.holdings else [])])
    if not syms:
        raise AlertError("which tickers? alerts.py quote TSLA KO, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, "offline" if a.offline else "nse")
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close ₹%.2f)" % (100.0 * (q["price"] / q["prev_close"] - 1), q["prev_close"])
                print("%-6s ₹%.2f%s  %s  [%s]" % (s, q["price"], chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([nse_data.nse_symbol(r.get("symbol", "")) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    signals = {}
    if any(r.get("type") in CATALYST_TYPES for r in watched):
        items = india_signals.load_all()
        for r in watched:
            if r.get("type") in CATALYST_TYPES:
                sym = nse_data.nse_symbol(r["symbol"])
                sig = india_signals.signals_for(sym, to_ist(now), items=items)
                signals[sym] = sig["catalysts"] + sig["smart_money"]
    rings, state = evaluate(rules, quotes, status, state, now, signals)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, "offline" if a.offline else "nse")
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"],
                                              status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s ₹%.2f" % (s, quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A bracket's stop-loss and take-profit already sit at "
                  "the broker: check get_open_position and record any fill (ticket.py fill). To close a "
                  "position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the broker's bracket orders are what actually stop you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.catalysts:
        sym = nse_data.nse_symbol(a.catalysts)
        rule = {"id": a.id or "%s-catalysts" % sym.lower(), "type": "catalyst", "symbol": sym,
                "note": "new filings and smart-money items (trader-news / trader-pelosi)"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an India time (IST) like 15:15")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read nse-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = India time, IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--catalysts", metavar="SYMBOL",
                   help="ring on every new filing or smart-money item for SYMBOL (needs news.py / smartmoney.py runs)")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, India time (IST)")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves earlier on an early close (NSE has none scheduled)")
    p.add_argument("--date", help="watch this rule on this India date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
