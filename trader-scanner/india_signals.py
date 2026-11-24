"""What the Indian filings scanner (trader-news) and smart money tracker (trader-pelosi) know about
a symbol, for the trading tools: scanner, entry, exit, time machine, risk and alerts.

Standard library only, no network, no language model. It reads the files those two tools write:
  trader/catalysts/latest.json      the last news.py run (tags, numbers, board meetings ahead)
  trader/catalysts/alerts.csv       every catalyst news.py has flagged (history across runs)
  trader/smart_money/latest.json    the last smartmoney.py run
  trader/smart_money/alerts.csv     every smart-money item flagged (history across runs)
  trader/orders/latest.json         the last govt_orders_tracker.py run (orders, cancellations, lag)
  trader/orders/alerts.csv          every order item it has flagged
When a file is missing the answer is simply empty; run /trader-news and /trader-pelosi to fill them.

The same copy lives in every tool folder that uses it (a repo test keeps them identical).

    signals_for(symbol, now)  -> {"catalysts": [...], "smart_money": [...]}   (recent, newest first)
    risk_flags(signals, now)  -> [{"flag", "why", "link"}]   things a trade should know about
    summary(signals, flags)   -> one short line for a printout
"""

import csv
import datetime as _dt
import json
import os
import re

BASE = "trader"
IST = _dt.timedelta(hours=5, minutes=30)


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0) + IST


def _bare(sym):
    s = str(sym or "").strip().upper()
    return s[:-3] if s.endswith((".NS", ".BO")) else s


def _read_json_items(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return list((doc or {}).get("items") or [])
    except (OSError, ValueError):
        return []


def _read_csv(path):
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _norm(row, kind):
    """A JSON item or a CSV row -> one shape."""
    tags = row.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(" | ") if t]
    flags = row.get("flags")
    if isinstance(flags, str):
        flags = [f for f in flags.split(" | ") if f]
    nums = row.get("numbers")
    if isinstance(nums, str):
        try:
            nums = json.loads(nums) if nums else {}
        except ValueError:
            nums = {}
    hi = row.get("highlight")
    return {"kind": kind, "id": row.get("id") or "", "symbol": _bare(row.get("symbol")),
            "company": row.get("company") or "", "time": row.get("time") or "",
            "tag": row.get("tag") or row.get("category") or "", "tags": tags or [],
            "category": row.get("category") or "", "title": row.get("title") or "",
            "detail": row.get("detail") or "", "link": row.get("link") or "", "flags": flags or [],
            "numbers": nums or {}, "event_date": row.get("event_date") or "",
            "highlight": hi is True or str(hi) == "True", "source": row.get("source") or ""}


def load_all(base=BASE):
    """Every catalyst and smart-money item on disk, one per id."""
    out = {}
    for kind, folder in (("catalyst", "catalysts"), ("smart_money", "smart_money"), ("catalyst", "orders")):
        rows = _read_csv(os.path.join(base, folder, "alerts.csv")) + _read_json_items(os.path.join(base, folder, "latest.json"))
        for r in rows:
            it = _norm(r, kind)
            if it["id"]:
                out[it["id"]] = it   # the JSON row comes last and carries the most detail
    return list(out.values())


def signals_for(symbol, now=None, days=3, company=None, base=BASE, items=None):
    """The symbol's catalysts and smart-money items of the last `days` days, plus any board meeting
    still ahead. Matched on the NSE symbol, or on the company name when an item has no symbol."""
    sym = _bare(symbol)
    now = now or _now()
    since = (now - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    name = (company or "").strip().lower()
    cats, money = [], []
    for it in items if items is not None else load_all(base):
        mine = (it["symbol"] and it["symbol"] == sym) or (name and it["company"].strip().lower() == name)
        if not mine:
            continue
        ahead = it["event_date"] and it["event_date"] >= today
        if not ahead and (it["time"] or "")[:10] < since:
            continue
        (cats if it["kind"] == "catalyst" else money).append(it)
    key = lambda i: i["time"] or ""  # noqa: E731
    return {"symbol": sym, "catalysts": sorted(cats, key=key, reverse=True),
            "smart_money": sorted(money, key=key, reverse=True)}


RESULTS = re.compile(r"financial results?|quarterly results|un-?audited|audited", re.I)


def risk_flags(signals, now=None):
    """What a trade in this symbol should know about today. Never a block: the tools only warn."""
    now = now or _now()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    out = []

    def add(flag, why, link=""):
        if flag not in [f["flag"] for f in out]:
            out.append({"flag": flag, "why": why, "link": link})
    for c in signals.get("catalysts", []):
        text = "%s %s" % (c["title"], c["detail"])
        if c["event_date"] and RESULTS.search(text):
            if c["event_date"] in (today, tomorrow):
                add("results due %s" % c["event_date"],
                    "board meeting on %s to consider results: an overnight gap can jump any stop" % c["event_date"], c["link"])
            else:
                add("results meeting %s" % c["event_date"], "results scheduled for %s" % c["event_date"], c["link"])
        elif c["event_date"]:
            add("board meeting %s" % c["event_date"], c["title"], c["link"])
        if "results" in c["tags"] and not c["event_date"] and (c["time"] or "")[:10] == today:
            n = c["numbers"] or {}
            extra = ""
            if n.get("profit_yoy") is not None or n.get("revenue_yoy") is not None:
                extra = " (revenue %s%%, profit %s%% on last year)" % (n.get("revenue_yoy"), n.get("profit_yoy"))
            add("results out today", "results filed today%s: expect a volatile session" % extra, c["link"])
        if re.search(r"added to (?:LT-|ST-)?ASM|added to GSM|ASM stage|GSM stage|stage .* -> ", text):
            add("surveillance", c["title"], c["link"])
        m = re.search(r"price band \S+%? -> (\d+)%", text)
        if m and int(m.group(1)) <= 5:
            add("price band %s%%" % m.group(1), "%s: the stock can lock at its circuit" % c["title"], c["link"])
        if re.search(r"resignation of (?:the )?(?:statutory )?auditor", text, re.I):
            add("auditor resigned", c["title"], c["link"])
        if "buyback" in c["tags"] and re.search(r"ex[- ]date", text, re.I):
            add("ex-date", "%s: the price adjusts on the ex-date" % c["title"], c["link"])
        if "ORDER LOSS / CANCELLATION" in c["flags"]:
            add("order loss / cancellation", c["title"], c["link"])
        if "EXECUTION LAG WATCH" in c["flags"]:
            add("execution lag watch", c["title"], c["link"])
        if "DEBARMENT" in c["flags"]:
            add("debarment", c["title"], c["link"])
    for s in signals.get("smart_money", []):
        flags = " ".join(s["flags"]).lower()
        text = "%s %s" % (s["title"], s["detail"])
        if "promoter" in flags and ("sale" in flags or "sell" in flags or re.search(r"\bsold\b", text)):
            add("promoter selling", s["title"], s["link"])
        if "invoked" in flags:
            add("pledge invoked", s["title"], s["link"])
        if "increased" in flags or "fresh pledge" in flags:
            add("promoter pledge up", s["title"], s["link"])
        if s["highlight"] and ("institution" in flags or "insider open-market buy" in flags or "crossed 5%" in flags):
            add("smart money buying", s["title"], s["link"])
    return out


def summary(signals, flags=None, width=120):
    """One line: flags first, then the latest catalyst tags and smart-money items."""
    parts = [f["flag"] for f in (flags or [])]
    for c in signals.get("catalysts", [])[:3]:
        t = c["tag"] if c["tag"] and c["tag"] != "other" else ""
        if t and t not in parts:
            parts.append(t)
    for s in signals.get("smart_money", [])[:2]:
        if s["category"] and s["category"] not in parts:
            parts.append(s["category"])
    line = ", ".join(parts)
    return line if len(line) <= width else line[:width - 3] + "..."


def describe(symbol, now=None, days=3, base=BASE):
    """Lines for a printout: the flags with their reasons, then the latest items. [] when nothing."""
    sig = signals_for(symbol, now, days, base=base)
    flags = risk_flags(sig, now)
    if not (sig["catalysts"] or sig["smart_money"]):
        return [], sig, flags
    out = ["Catalysts for %s (trader-news, trader-pelosi, last %d days):" % (sig["symbol"], days)]
    for f in flags:
        out.append("  ! %s: %s" % (f["flag"], f["why"]))
    for it in (sig["catalysts"][:4] + sig["smart_money"][:3]):
        out.append("  - %s %s" % ((it["time"] or "")[:16], it["title"][:110]))
    return out, sig, flags
