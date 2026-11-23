#!/usr/bin/env python3
"""Indian Smart Money & Institutional Tracker (trader-pelosi): who is buying and selling,
from official NSE / AMFI data, with no language model anywhere.

    python3 smartmoney.py                      # every source, new items only -> terminal + trader/smart_money/
    python3 smartmoney.py --only deals,fiidii  # some sources
    python3 smartmoney.py --days 7 --all       # a wider window, items already flagged too
    python3 smartmoney.py --dry-run            # fetch and parse live, write nothing, remember nothing
    python3 smartmoney.py --json
    python3 smartmoney.py amfi-links           # AMFI's directory of fund-house portfolio pages
    python3 smartmoney.py amfi --file ppfas_oct.xlsx --amc "PPFAS MF" --month 2026-10

Sources (each one is independent: one failing does not stop the rest):
  deals         NSE bulk and block deals of the day (www.nseindia.in/api/snapshot-capital-market-largedeal),
                checked against the day's delivery % (nsearchives sec_bhavdata_full_<DDMMYYYY>.csv).
                Intraday noise is filtered out: a client that both bought and sold the same stock
                the same day (a round trip), and trading / broking desks on a low-delivery day.
                Highlighted: block deals, institutions (funds, insurers, FPIs, banks, ODIs) and
                big high-delivery deals.
  insider       SEBI PIT (Prohibition of Insider Trading) disclosures: NSE's PIT API, NSE's Insider
                Trading RSS (each item's XBRL is read: person category, buy / sell, mode, value) and
                announcements citing Regulation 7. Highlighted: promoter / promoter group /
                director / KMP buys through the open market.
  sast          SEBI SAST (Takeover Code) Reg 29 disclosures (www.nseindia.in/api/corporate-sast-reg29):
                acquisitions crossing 5 %, promoter acquisitions and sales.
  pledge        Promoter pledge / encumbrance: NSE's pledge data compared with the last run (a new
                pledge, a bigger one, a release, a full revocation), plus Reg 31 announcements.
  shareholding  Quarterly shareholding patterns: NSE's Shareholding RSS (each XBRL: promoter, FII,
                DII, public %) compared with the company's previous quarter, and promoter-stake
                changes from NSE's shareholding master.
  fiidii        The day's FII / FPI and DII cash-market buy, sell and net (provisional, all exchanges).
  amfi          Monthly mutual fund portfolios. AMFI lists each fund house's disclosure page;
                the files are each fund house's own Excel. Give them with `amfi --file/--url` or in
                trader/amfi_portfolios.json; each month is compared with the last: fresh buys
                (a stock new to the fund house) and stake increases.

Output: the terminal; trader/smart_money/latest.json (this run); new items appended to
trader/smart_money/alerts.csv. trader/state/smart_money.json remembers every flagged item (90 days)
and the snapshots the diffs need, so an item is never flagged twice. --offline DIR reads saved
answers instead of the network (the tests use it).

This reads public disclosures. It is not advice, and nothing here places an order.
"""

import argparse
import datetime as _dt
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_http as ih  # noqa: E402

SOURCES = ("deals", "insider", "sast", "pledge", "shareholding", "fiidii", "amfi")
OUT_DIR = os.path.join("trader", "smart_money")
STATE = os.path.join("trader", "state", "smart_money.json")
AMFI_CONFIG = os.path.join("trader", "amfi_portfolios.json")
AMFI_DIRECTORY = "https://www.amfiindia.com/online-center/portfolio-disclosure"
CSV_FIELDS = ("time", "category", "symbol", "company", "title", "detail", "value_cr", "highlight", "flags",
              "source", "link", "id")
CRORE = 1e7

INSTITUTION = re.compile(
    r"MUTUAL FUND|\bMF\b|ASSET MANAGEMENT|\bAMC\b|INSURANCE|\bLIC\b|LIFE INSURANCE|PENSION|PROVIDENT|"
    r"\bFUNDS?\b|SICAV|\bFPI\b|\bODI\b|\bPLC\b|SOCIETE GENERALE|GOLDMAN|MORGAN STANLEY|NOMURA|CITIGROUP|"
    r"BNP PARIBAS|J\.?P\.? ?MORGAN|MERRILL|BOFA|HSBC|\bUBS\b|CLSA|VANGUARD|BLACKROCK|NORGES|"
    r"GOVERNMENT OF|MONETARY AUTHORITY|INVESTMENT (?:CORPORATION|AUTHORITY)|SOVEREIGN|ENDOWMENT|BANK\b|"
    r"VENTURES? FUND|CAPITAL FUND|\bAIF\b|TRUSTEE", re.I)
DESK = re.compile(r"TRADING|STOCK ?BROK|SHARE ?BROK|BROKING|SECURITIES (?:PVT|PRIVATE|LTD|LIMITED)|"
                  r"COMMODIT|FINVEST|DERIVATIVE|ARBITRAGE|QUANT|CAPITAL MARKETS", re.I)
INSIDER_CAT = re.compile(r"promoter|director|\bKMP\b|key managerial|designated person", re.I)
OPEN_MARKET = re.compile(r"market", re.I)


# ---------------------------------------------------------------- helpers

def item(**kw):
    base = {"id": "", "source": "", "category": "", "symbol": "", "company": "", "title": "", "detail": "",
            "value_cr": None, "time": "", "link": "", "flags": [], "highlight": False, "data": {}}
    base.update(kw)
    return base


def cr(x):
    return None if x is None else round(x / CRORE, 2)


def ddmmyyyy(d):
    return d.strftime("%d-%m-%Y")


def xbrl_facts(text):
    """XBRL -> {context: {tag: value}}, prefixes dropped."""
    out = {}
    for m in re.finditer(r"<[\w-]+:(\w+)\s+contextRef=\"([^\"]+)\"[^>]*>([^<]*)<", text):
        out.setdefault(m.group(2), {})[m.group(1)] = html.unescape(m.group(3).strip())
    return out


def xbrl_head(facts, keys):
    """The first non-empty value of each key, across all contexts."""
    head = {}
    for f in facts.values():
        for k in keys:
            if f.get(k) and not head.get(k):
                head[k] = f[k]
    return head


# ---------------------------------------------------------------- deals

def delivery_map(client, day):
    """{SYMBOL: delivery %} for EQ rows of NSE's full bhavcopy of `day` (a date)."""
    url = "%s/products/content/sec_bhavdata_full_%s.csv" % (ih.NSE_ARCHIVE, day.strftime("%d%m%Y"))
    text = client.get_text(url, "sec_bhavdata_full_%s.csv" % day.strftime("%d%m%Y"))
    out = {}
    lines = text.splitlines()
    if not lines or "DELIV_PER" not in lines[0]:
        raise ih.FeedError("the bhavcopy for %s has no delivery column" % day)
    head = [h.strip() for h in lines[0].split(",")]
    for ln in lines[1:]:
        row = dict(zip(head, [c.strip() for c in ln.split(",")]))
        if row.get("SERIES") in ("EQ", "BE", "BZ", "SM", "ST") and ih.num(row.get("DELIV_PER")) is not None:
            out[row["SYMBOL"]] = ih.num(row["DELIV_PER"])
    return out


def classify_deals(doc, deliv, min_value_cr=1.0, high_deliv=50.0, low_deliv=30.0):
    """The large-deals snapshot -> (items, counts). Pure."""
    rows = []
    for kind, key in (("block", "BLOCK_DEALS_DATA"), ("bulk", "BULK_DEALS_DATA")):
        for r in doc.get(key) or []:
            if not r.get("symbol") or not r.get("clientName"):
                continue
            rows.append(dict(r, kind=kind))
    sides = {}
    for r in rows:
        sides.setdefault((r["kind"], r["symbol"], r["clientName"].upper()), set()).add((r.get("buySell") or "").upper())
    counts = {"deals": len(rows), "round_trip": 0, "desk_low_delivery": 0, "small": 0, "kept": 0}
    out = []
    for r in rows:
        qty, price = ih.num(r.get("qty")) or 0, ih.num(r.get("watp")) or 0
        value = cr(qty * price)
        name = r["clientName"].strip()
        d = deliv.get(r["symbol"])
        inst = bool(INSTITUTION.search(name))
        desk = bool(DESK.search(name)) and not inst
        if len(sides[(r["kind"], r["symbol"], name.upper())]) > 1:
            counts["round_trip"] += 1
            continue                       # bought and sold the same day: intraday, not a position
        if desk and (d is None or d < low_deliv):
            counts["desk_low_delivery"] += 1
            continue                       # a trading desk on a day most volume was not delivered
        if r["kind"] == "bulk" and not inst and (value or 0) < min_value_cr:
            counts["small"] += 1
            continue
        flags = [r["kind"] + " deal"]
        if inst:
            flags.append("institution")
        if d is not None:
            flags.append("delivery %.0f%%" % d)
            if d >= high_deliv:
                flags.append("high delivery")
        hi = r["kind"] == "block" or inst or (d is not None and d >= high_deliv and (value or 0) >= 5)
        side = (r.get("buySell") or "").upper()
        out.append(item(id="deal|%s|%s|%s|%s|%s|%s" % (r["kind"], r.get("date"), r["symbol"], name.upper(), side,
                                                        r.get("qty")),
                        source="NSE large deals", category="Bulk/Block deal", symbol=r["symbol"],
                        company=r.get("name") or "", time=ih.parse_when(r.get("date")),
                        title="%s %s %s of %s (%s deal)" % (name, "bought" if side == "BUY" else "sold",
                                                           "{:,.0f}".format(qty), r["symbol"], r["kind"]),
                        detail="at Rs %s avg, about Rs %s cr; %s" % (r.get("watp"), value,
                                                                      ("delivery %.1f%% of the day's volume" % d)
                                                                      if d is not None else "delivery % not out yet"),
                        value_cr=value, flags=flags, highlight=hi,
                        link=ih.NSE + "/market-data/large-deals", data={"side": side, "qty": qty, "price": price}))
    counts["kept"] = len(out)
    return out, counts


def collect_deals(client, args, notes):
    doc = client.nse_api("/api/snapshot-capital-market-largedeal", "nse_largedeal.json")
    day = _dt.datetime.strptime(doc.get("as_on_date") or ih.now_ist().strftime("%d-%b-%Y"), "%d-%b-%Y").date()
    try:
        deliv = delivery_map(client, day)
    except ih.FeedError as e:
        deliv = {}
        notes.append("deals: no delivery data for %s yet (%s); delivery checks skipped" % (day, e))
    items, counts = classify_deals(doc, deliv, args.min_deal_cr)
    notes.append("deals %s: %d bulk/block deals, %d round trips and %d trading-desk low-delivery deals "
                 "filtered as intraday noise, %d small, %d kept" % (day, counts["deals"], counts["round_trip"],
                                                                     counts["desk_low_delivery"], counts["small"],
                                                                     counts["kept"]))
    return items


# ---------------------------------------------------------------- insider (SEBI PIT)

def insider_item(rec, source, link):
    """One PIT record (normalised dict) -> item."""
    cat, mode, typ = rec.get("category") or "", rec.get("mode") or "", (rec.get("type") or "").lower()
    buy = typ in ("buy", "acquisition", "purchase", "pledge revoke", "revoke")
    value = ih.num(rec.get("value"))
    hi = bool(INSIDER_CAT.search(cat)) and bool(OPEN_MARKET.search(mode)) and buy
    flags = [f for f in (cat, mode, typ) if f]
    if hi:
        flags.append("insider open-market buy")
    return item(id="pit|%s|%s|%s|%s|%s" % (rec.get("symbol"), rec.get("person"), typ, rec.get("qty"), rec.get("date")),
                source=source, category="Insider (SEBI PIT)", symbol=rec.get("symbol") or "",
                company=rec.get("company") or "", time=rec.get("time") or ih.parse_when(rec.get("date")),
                title="%s (%s) %s %s shares of %s" % (rec.get("person") or "?", cat or "?",
                                                    "bought" if buy else (typ or "traded"),
                                                    "{:,.0f}".format(ih.num(rec.get("qty")) or 0),
                                                    rec.get("symbol") or rec.get("company") or "?"),
                detail="mode %s, value Rs %s cr, holding %s%% -> %s%%" % (mode or "?", cr(value),
                                                                         rec.get("before_pct"), rec.get("after_pct")),
                value_cr=cr(value), flags=flags, highlight=hi, link=link, data=rec)


def parse_insider_xbrl(text):
    """An NSE PIT XBRL -> [normalised records]."""
    facts = xbrl_facts(text)
    head = xbrl_head(facts, ("NSESymbol", "NameOfTheCompany"))
    out = []
    for ctx, f in sorted(facts.items()):
        if "NameOfThePerson" not in f:
            continue
        pct_b = ih.num(f.get("SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding"))
        pct_a = ih.num(f.get("SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding"))
        out.append({"symbol": head.get("NSESymbol") or "", "company": head.get("NameOfTheCompany") or "",
                    "person": f.get("NameOfThePerson"), "category": f.get("CategoryOfPerson"),
                    "type": f.get("SecuritiesAcquiredOrDisposedTransactionType"),
                    "mode": f.get("ModeOfAcquisitionOrDisposal"),
                    "qty": f.get("SecuritiesAcquiredOrDisposedNumberOfSecurity"),
                    "value": f.get("SecuritiesAcquiredOrDisposedValueOfSecurity") or f.get("ValueInAggregate"),
                    "date": f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate")
                    or f.get("DateOfIntimationToCompany"),
                    "before_pct": pct_b, "after_pct": pct_a, "instrument": f.get("TypeOfInstrument")})
    return out


def parse_pit_api(doc):
    """NSE's corporates-pit answer -> normalised records (the field names NSE uses on its PIT page)."""
    out = []
    for r in (doc or {}).get("data") or []:
        out.append({"symbol": r.get("symbol"), "company": r.get("company"), "person": r.get("acqName"),
                    "category": r.get("personCategory"), "type": r.get("tdpTransactionType"),
                    "mode": r.get("acqMode"), "qty": r.get("secAcq"), "value": r.get("secVal"),
                    "date": r.get("acqtoDt") or r.get("date"), "before_pct": r.get("befAcqSharesPer"),
                    "after_pct": r.get("afterAcqSharesPer"), "link": r.get("xbrl")})
    return out


def collect_insider(client, args, state, notes, announcements):
    items = []
    to = ih.now_ist().date()
    frm = to - _dt.timedelta(days=args.days)
    try:
        doc = client.nse_api("/api/corporates-pit?index=equities&from_date=%s&to_date=%s" % (ddmmyyyy(frm), ddmmyyyy(to)),
                             "nse_pit.json")
        recs = parse_pit_api(doc)
        items += [insider_item(r, "NSE PIT API", r.get("link") or ih.NSE + "/companies-listing/corporate-filings-insider-trading")
                  for r in recs]
        if not recs:
            notes.append("insider: NSE's PIT API returned no records for %s to %s" % (frm, to))
    except ih.FeedError as e:
        notes.append("insider: NSE PIT API: %s" % e)
    try:
        feed = ih.parse_rss(client.get_bytes(ih.NSE_ARCHIVE + "/content/RSS/Insider_Trading.xml", "rss_Insider_Trading.xml"))
        fetched = 0
        newest = max((e["time"] for e in feed if e["time"]), default="")
        for e in feed:
            if not e["link"].lower().endswith(".xml") or fetched >= args.max_xbrl:
                continue
            key = "pitxbrl|" + e["link"]
            if not state.is_new(key) and not args.all:
                continue
            fetched += 1
            try:
                recs = parse_insider_xbrl(client.get_text(e["link"], ih.safe_name(e["link"].rsplit("/", 1)[-1])))
            except ih.FeedError as err:
                notes.append("insider: %s" % err)
                continue
            for r in recs:
                r["time"] = e["time"]
                items.append(insider_item(r, "NSE Insider Trading RSS", e["link"]))
        notes.append("insider: NSE Insider Trading RSS has %d items, newest %s" % (len(feed), newest or "undated"))
    except ih.FeedError as e:
        notes.append("insider: NSE Insider Trading RSS: %s" % e)
    for a in announcements:
        text = a.get("attchmntText") or ""
        if re.search(r"Insider Trading|Regulation 7\s*\(\s*[12]\s*\)|\bPIT\b", text + " " + (a.get("desc") or ""), re.I):
            items.append(item(id="pitann|" + str(a.get("seq_id") or a.get("attchmntFile")), source="NSE announcements",
                              category="Insider (SEBI PIT)", symbol=a.get("symbol") or "",
                              company=a.get("sm_name") or "", time=ih.parse_when(a.get("an_dt")),
                              title="%s: insider-trading disclosure" % (a.get("symbol") or a.get("sm_name")),
                              detail=text[:300], link=a.get("attchmntFile") or "", flags=["PIT disclosure"]))
    return items


# ---------------------------------------------------------------- SAST and pledges

def sast_items(doc, min_pct=0.1):
    """Reg 29 disclosures -> items. Highlighted: open-market moves that cross 5 %, promoter buys (or
    sales) of at least `min_pct` % of the company, and institutional acquisitions."""
    out = []
    for r in (doc or {}).get("data") or []:
        acq = (r.get("acqSaleType") or "").lower().startswith("acq")
        after, moved = ih.num(r.get("totAftShare")), ih.num(r.get("totAcqShare") if acq else r.get("totSaleShare"))
        before = None if after is None or moved is None else round(after - moved if acq else after + moved, 2)
        promoter = (r.get("promoterType") or "").upper() == "Y"
        crossed = acq and after is not None and before is not None and before < 5 <= after
        flags = [r.get("regType") or "SAST", "acquisition" if acq else "sale"]
        if promoter:
            flags.append("promoter")
        if crossed:
            flags.append("crossed 5%")
        if not acq and before is not None and after is not None and after < 5 <= before:
            flags.append("fell below 5%")
        open_market = "market" in (r.get("acquisitionMode") or "").lower()
        inst = bool(INSTITUTION.search(r.get("acquirerName") or ""))
        if open_market:
            flags.append("open market")
        # highlighted: market moves only; inter-se transfers, gifts and inheritances are bookkeeping
        hi = open_market and (crossed or (promoter and (moved or 0) >= (min_pct if acq else 1.0)) or (inst and acq))
        out.append(item(id="sast|" + str(r.get("application_no") or (r.get("symbol"), r.get("acquirerName"), r.get("sysTime"))),
                        source="NSE SAST Reg 29", category="SAST / Takeover", symbol=r.get("symbol") or "",
                        company=r.get("company") or "", time=ih.parse_when(r.get("timestamp") or r.get("sysTime")),
                        title="%s %s %s%% of %s (%s)" % (r.get("acquirerName"), "acquired" if acq else "sold",
                                                        r.get("totAcqShare") if acq else r.get("totSaleShare"),
                                                        r.get("symbol"), r.get("acquisitionMode") or "mode n/a"),
                        detail="holding %s%% -> %s%%, %s" % (before, after, r.get("acquirerDate") or ""),
                        link=r.get("attachement") or "", flags=flags, highlight=hi, data={"before": before, "after": after}))
    return out


def pledge_snapshot(doc):
    snap = {}
    for r in (doc or {}).get("data") or []:
        name = (r.get("comName") or "").strip()
        if name:
            snap[name] = {"num": ih.num(r.get("numSharesPledged")) or 0.0, "pct": ih.num(r.get("percSharesPledged")) or 0.0,
                          "promoter_pct": ih.num(r.get("percPromoterHolding")), "dt": r.get("broadcastDt"),
                          "shp": r.get("shp")}
    return snap


def pledge_changes(prev, cur):
    """Two pledge snapshots -> items for every company whose pledged shares changed."""
    out = []
    for name, c in sorted(cur.items()):
        p = (prev or {}).get(name)
        if p is None or abs(c["num"] - p["num"]) < 1:
            continue
        if p["num"] == 0:
            what = "fresh pledge"
        elif c["num"] == 0:
            what = "pledge fully revoked"
        elif c["num"] > p["num"]:
            what = "pledge increased"
        else:
            what = "pledge reduced (released)"
        out.append(item(id="pledge|%s|%s|%s" % (name, c.get("dt"), c["num"]), source="NSE pledge data",
                        category="Promoter pledge", company=name, time=ih.parse_when(c.get("dt")),
                        title="%s: %s" % (name, what),
                        detail="pledged shares %s -> %s (%.2f%% -> %.2f%% of promoter holding)"
                               % ("{:,.0f}".format(p["num"]), "{:,.0f}".format(c["num"]), p["pct"], c["pct"]),
                        flags=[what], highlight=True, link=ih.NSE + "/companies-listing/corporate-filings-pledged-data"))
    return out


def pledge_announcements(announcements):
    out = []
    for a in announcements:
        text = (a.get("attchmntText") or "") + " " + (a.get("desc") or "")
        if not re.search(r"Regulation 31\b|encumbranc|(?:creation|release|revocation|invocation) of (?:the )?pledge|"
                         r"pledge (?:creat|releas|revo|invo)", text, re.I):
            continue
        if re.search(r"releas|revoc|revok|discharg", text, re.I):
            what = "pledge released / revoked"
        elif re.search(r"invoc|invok", text, re.I):
            what = "pledge invoked"
        else:
            what = "pledge / encumbrance created"
        out.append(item(id="pledgeann|" + str(a.get("seq_id") or a.get("attchmntFile")), source="NSE announcements",
                        category="Promoter pledge", symbol=a.get("symbol") or "", company=a.get("sm_name") or "",
                        time=ih.parse_when(a.get("an_dt")), title="%s: %s" % (a.get("symbol"), what),
                        detail=(a.get("attchmntText") or "")[:300], flags=[what, "Reg 31"],
                        highlight="invoked" in what or "released" in what, link=a.get("attchmntFile") or ""))
    return out


# ---------------------------------------------------------------- shareholding

SHP_CONTEXTS = {"promoter": "ShareholdingOfPromoterAndPromoterGroup_ContextI", "fii": "InstitutionsForeign_ContextI",
                "dii": "InstitutionsDomestic_ContextI", "public": "PublicShareholding_ContextI"}


def parse_shp_xbrl(text):
    """A shareholding-pattern XBRL -> {symbol, company, date, promoter, fii, dii, public} (percent)."""
    facts = xbrl_facts(text)
    head = xbrl_head(facts, ("Symbol", "NameOfTheCompany", "DateOfReport"))
    out = {"symbol": head.get("Symbol") or "", "company": head.get("NameOfTheCompany") or "",
           "date": head.get("DateOfReport") or ""}
    for key, ctx in SHP_CONTEXTS.items():
        v = ih.num((facts.get(ctx) or {}).get("ShareholdingAsAPercentageOfTotalNumberOfShares"))
        out[key] = None if v is None else round(v * 100, 2)   # XBRL holds fractions
    if out["dii"] is None and out["promoter"] is not None:
        out["dii"] = 0.0     # a category with no holders is left out of the XBRL
    if out["fii"] is None and out["promoter"] is not None:
        out["fii"] = 0.0
    return out


def shp_item(cur, prev, link, threshold=1.0):
    shifts, flags = [], []
    for k, label in (("promoter", "Promoter"), ("fii", "FII"), ("dii", "DII")):
        if prev and cur.get(k) is not None and prev.get(k) is not None:
            d = round(cur[k] - prev[k], 2)
            if abs(d) >= 0.01:
                shifts.append("%s %+.2f pp" % (label, d))
            if abs(d) >= threshold:
                flags.append("%s %s" % (label, "up" if d > 0 else "down"))
    detail = "Promoter %s%%, FII %s%%, DII %s%%, public %s%%" % (cur.get("promoter"), cur.get("fii"), cur.get("dii"),
                                                                 cur.get("public"))
    if prev:
        detail += "; vs %s: %s" % (prev.get("date") or "last quarter", ", ".join(shifts) or "no change")
    else:
        detail += " (first quarter seen here: the next filing shows the shift)"
    return item(id="shp|%s|%s" % (cur.get("symbol") or cur.get("company"), cur.get("date")), source="NSE Shareholding RSS",
                category="Shareholding pattern", symbol=cur.get("symbol") or "", company=cur.get("company") or "",
                time=ih.parse_when(cur.get("date")), title="%s shareholding as on %s" % (cur.get("symbol") or cur.get("company"),
                                                                                     cur.get("date")),
                detail=detail, flags=flags, highlight=bool(flags), link=link, data={"now": cur, "before": prev})


def master_snapshot(rows):
    """NSE's shareholding master -> {company: {date, promoter, broadcast}} for each company's latest quarter."""
    snap = {}
    for r in rows or []:
        name, pr = r.get("name"), ih.num(r.get("pr_and_prgrp"))
        if not name or pr is None:
            continue
        when = ih.parse_when(r.get("date"))
        if name not in snap or when > snap[name]["when"]:
            snap[name] = {"date": r.get("date"), "when": when, "promoter": pr, "broadcast": r.get("broadcastDate")}
    return snap


def promoter_shifts(prev, cur, threshold=1.0):
    """Promoter-stake changes between the quarter saved last time and a newer quarter now.
    (NSE's master carries only each company's latest quarter, so the history is ours.)"""
    out = []
    for name, c in sorted(cur.items()):
        p = (prev or {}).get(name)
        if not p or p.get("when", "") >= c["when"]:
            continue
        d = round(c["promoter"] - p["promoter"], 2)
        if abs(d) < threshold:
            continue
        out.append(item(id="shpmaster|%s|%s" % (name, c["date"]), source="NSE shareholding master",
                        category="Shareholding pattern", company=name, time=ih.parse_when(c.get("broadcast")),
                        title="%s: promoter stake %s %.2f pp" % (name, "up" if d > 0 else "down", abs(d)),
                        detail="promoter and group %s%% (%s) -> %s%% (%s)" % (p["promoter"], p["date"], c["promoter"], c["date"]),
                        flags=["promoter %s" % ("up" if d > 0 else "down")], highlight=True,
                        link=ih.NSE + "/companies-listing/corporate-filings-shareholding-pattern"))
    return out


def collect_shareholding(client, args, state, notes):
    items = []
    snaps = state.snapshot("shareholding") or {}
    try:
        feed = ih.parse_rss(client.get_bytes(ih.NSE_ARCHIVE + "/content/RSS/Shareholding_Pattern.xml",
                                             "rss_Shareholding_Pattern.xml"))
        fetched = 0
        for e in feed:
            if not e["link"].lower().endswith(".xml") or fetched >= args.max_xbrl:
                continue
            if not state.is_new("shpxbrl|" + e["link"]) and not args.all:
                continue
            fetched += 1
            try:
                cur = parse_shp_xbrl(client.get_text(e["link"], ih.safe_name(e["link"].rsplit("/", 1)[-1])))
            except ih.FeedError as err:
                notes.append("shareholding: %s" % err)
                continue
            key = cur["symbol"] or cur["company"]
            prev = snaps.get(key)
            if prev and prev.get("date") == cur.get("date"):
                prev = prev.get("previous")
            items.append(shp_item(cur, prev, e["link"]))
            snaps[key] = dict(cur, previous=prev)
        notes.append("shareholding: NSE Shareholding RSS has %d filings" % len(feed))
    except ih.FeedError as e:
        notes.append("shareholding: NSE Shareholding RSS: %s" % e)
    state.set_snapshot("shareholding", snaps)
    try:
        cur = master_snapshot(client.nse_api("/api/corporate-share-holdings-master?index=equities", "nse_shp_master.json"))
        prev = state.snapshot("promoter")
        items += promoter_shifts(prev, cur)
        merged = dict(prev or {})
        for k, v in cur.items():   # keep the older quarter until a newer one arrives
            if k not in merged or v["when"] > merged[k]["when"]:
                merged[k] = v
        state.set_snapshot("promoter", merged)
        if prev is None:
            notes.append("shareholding: first run, the latest quarter of %d companies saved as the baseline "
                         "(promoter-stake changes show when the next quarter is filed)" % len(cur))
    except ih.FeedError as e:
        notes.append("shareholding: NSE shareholding master: %s" % e)
    return items


# ---------------------------------------------------------------- FII / DII

def fiidii_items(rows, alert_cr=1000.0):
    out = []
    for r in rows or []:
        net = ih.num(r.get("netValue"))
        if net is None:
            continue
        big = abs(net) >= alert_cr
        out.append(item(id="fiidii|%s|%s" % (r.get("date"), r.get("category")), source="NSE FII/DII (provisional)",
                        category="FII / DII flows", symbol=r.get("category") or "", time=ih.parse_when(r.get("date")),
                        title="%s net %s Rs %s cr on %s" % (r.get("category"), "bought" if net >= 0 else "sold",
                                                            "{:,.2f}".format(abs(net)), r.get("date")),
                        detail="buy Rs %s cr, sell Rs %s cr (cash market, all exchanges, provisional)"
                               % (r.get("buyValue"), r.get("sellValue")),
                        value_cr=net, flags=["net buy" if net >= 0 else "net sell"] + (["big day"] if big else []),
                        highlight=big, link=ih.NSE + "/reports/fii-dii"))
    return out


# ---------------------------------------------------------------- AMFI mutual fund portfolios

def amfi_directory(html):
    """AMFI's portfolio-disclosure page -> [{amc_site, url}] (each fund house publishes its own files)."""
    urls = sorted(set(u.rstrip("\\/") for u in re.findall(r"https?://[^\"'\s<>\\]+", html)))
    out, seen = [], set()
    for u in urls:
        host = u.split("/")[2].lower()
        if "amfiindia" in host or not re.search(r"portfolio|disclos", u, re.I) or re.search(r"\.(css|js|png|svg)$", u):
            continue
        key = (host, u)
        if key not in seen:
            seen.add(key)
            out.append({"amc_site": host, "url": u})
    return out


EQUITY_ISIN = re.compile(r"^INE[A-Z0-9]{4}01[A-Z0-9]{3}$")


def parse_portfolio_excel(path_or_bytes):
    """A fund house's monthly portfolio workbook (SEBI format: one sheet a scheme, a header row with
    ISIN and Quantity) -> {ISIN: {"name", "qty", "schemes"}} for equity holdings, summed over schemes."""
    import io
    import pandas as pd
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes
    sheets = pd.read_excel(src, sheet_name=None, header=None, dtype=str)
    out = {}
    for sheet, df in sheets.items():
        head_at, cols = None, {}
        for i in range(min(len(df), 40)):
            cells = [str(c).strip().lower() for c in df.iloc[i].tolist()]
            if any("isin" == c or c.startswith("isin") for c in cells) and any("quantity" in c for c in cells):
                head_at = i
                for j, c in enumerate(cells):
                    if c.startswith("isin"):
                        cols["isin"] = j
                    elif "quantity" in c and "qty" not in cols:
                        cols["qty"] = j
                    elif ("name of" in c or "instrument" in c or "company" in c) and "name" not in cols:
                        cols["name"] = j
                break
        if head_at is None or "isin" not in cols or "qty" not in cols:
            continue
        for _, row in df.iloc[head_at + 1:].iterrows():
            isin = str(row.iloc[cols["isin"]]).strip().upper()
            if not EQUITY_ISIN.match(isin):
                continue
            qty = ih.num(row.iloc[cols["qty"]])
            if not qty:
                continue
            h = out.setdefault(isin, {"name": str(row.iloc[cols["name"]]).strip() if "name" in cols else isin,
                                      "qty": 0.0, "schemes": []})
            h["qty"] += qty
            h["schemes"].append(str(sheet))
    return out


def amfi_changes(amc, month, cur, prev, prev_month, min_increase_pct=10.0):
    """This month's fund-house holdings against last month's -> fresh buys and stake increases."""
    out = []
    if prev is None:
        return out
    for isin, h in sorted(cur.items(), key=lambda kv: kv[1]["name"]):
        p = (prev or {}).get(isin)
        if p is None:
            what, pct = "fresh buy", None
        else:
            pct = (h["qty"] / p["qty"] - 1) * 100 if p["qty"] else None
            if pct is None or pct < min_increase_pct:
                continue
            what = "stake increased %.0f%%" % pct
        out.append(item(id="amfi|%s|%s|%s" % (amc, month, isin), source="AMC monthly portfolio (via AMFI directory)",
                        category="Mutual fund portfolio", company=h["name"], symbol=isin, time=month + "-01 00:00",
                        title="%s: %s in %s" % (amc, what, h["name"]),
                        detail="%s shares in %d scheme(s) at the end of %s%s" % (
                            "{:,.0f}".format(h["qty"]), len(h["schemes"]), month,
                            ("; %s shares at the end of %s" % ("{:,.0f}".format(p["qty"]), prev_month)) if p else
                            ("; none at the end of %s" % prev_month)),
                        flags=["fresh buy" if p is None else "stake up"], highlight=True,
                        data={"isin": isin, "qty": h["qty"], "prev_qty": p["qty"] if p else 0, "schemes": h["schemes"]}))
    return out


def amfi_ingest(client, state, entries, notes):
    """[{amc, month, file|url}] -> items. Each fund house's month is stored for next month's diff."""
    snaps = state.snapshot("amfi") or {}
    items = []
    for e in entries:
        amc, month = e.get("amc") or "?", e.get("month") or ""
        if not re.match(r"^\d{4}-\d{2}$", month):
            notes.append("amfi: %s needs \"month\": \"YYYY-MM\"" % amc)
            continue
        try:
            if e.get("file"):
                holdings = parse_portfolio_excel(e["file"])
            elif e.get("url"):
                holdings = parse_portfolio_excel(client.get_bytes(e["url"], ih.safe_name(e["url"]), max_bytes=40 * 1048576))
            else:
                notes.append("amfi: %s %s has no file or url" % (amc, month))
                continue
        except Exception as err:  # a bad workbook must not stop the other sources
            notes.append("amfi: %s %s: %s" % (amc, month, str(err)[:200]))
            continue
        if not holdings:
            notes.append("amfi: %s %s: no equity holdings found (no sheet with an ISIN and a Quantity column)" % (amc, month))
            continue
        hist = snaps.setdefault(amc, {})
        earlier = sorted(m for m in hist if m < month)
        prev_month = earlier[-1] if earlier else None
        items += amfi_changes(amc, month, holdings, hist.get(prev_month) if prev_month else None, prev_month)
        hist[month] = {k: {"name": v["name"], "qty": v["qty"], "schemes": v["schemes"][:20]} for k, v in holdings.items()}
        for m in sorted(hist)[:-3]:
            del hist[m]          # keep the last three months
        notes.append("amfi: %s %s: %d equity holdings%s" % (amc, month, len(holdings),
                                                          "" if prev_month else " (first month: the baseline)"))
    state.set_snapshot("amfi", snaps)
    return items


# ---------------------------------------------------------------- the run

def nse_announcements(client, days, notes):
    to = ih.now_ist().date()
    frm = to - _dt.timedelta(days=days)
    try:
        return client.nse_api("/api/corporate-announcements?index=equities&from_date=%s&to_date=%s"
                              % (ddmmyyyy(frm), ddmmyyyy(to)), "nse_announcements.json") or []
    except ih.FeedError as e:
        notes.append("NSE announcements: %s" % e)
        return []


def collect(client, args, state):
    only = set(args.only.split(",")) if args.only else set(SOURCES)
    bad = only - set(SOURCES)
    if bad:
        raise SystemExit("smartmoney.py: unknown source(s) %s; choose from %s" % (", ".join(sorted(bad)), ", ".join(SOURCES)))
    notes, items = [], []
    anns = nse_announcements(client, args.days, notes) if only & {"insider", "pledge"} else []

    def run(name, fn):
        if name in only:
            try:
                items.extend(fn())
            except ih.FeedError as e:
                notes.append("%s: %s" % (name, e))
            except Exception as e:   # one broken source never stops the others
                notes.append("%s: unexpected %s: %s" % (name, type(e).__name__, str(e)[:200]))

    run("deals", lambda: collect_deals(client, args, notes))
    run("insider", lambda: collect_insider(client, args, state, notes, anns))
    run("sast", lambda: sast_items(client.nse_api("/api/corporate-sast-reg29?index=equities", "nse_sast.json"),
                                   args.min_sast_pct))

    def pledge():
        cur = pledge_snapshot(client.nse_api("/api/corporate-pledgedata?index=equities", "nse_pledge.json"))
        prev = state.snapshot("pledge")
        state.set_snapshot("pledge", cur)
        if prev is None:
            notes.append("pledge: first run, %d companies saved as the baseline (changes show from the next run)" % len(cur))
        return pledge_changes(prev, cur) + pledge_announcements(anns)
    run("pledge", pledge)
    run("shareholding", lambda: collect_shareholding(client, args, state, notes))
    run("fiidii", lambda: fiidii_items(client.nse_api("/api/fiidiiTradeReact", "nse_fiidii.json"), args.fii_alert_cr))

    def amfi():
        cfg = ih.load_json(args.amfi_config) or []
        if not cfg:
            notes.append("amfi: no fund-house files to read (add them to %s or run `smartmoney.py amfi --file ...`)"
                         % args.amfi_config)
            return []
        return amfi_ingest(client, state, cfg, notes)
    run("amfi", amfi)
    mine = ih.my_symbols()
    for it in items:
        it["mine"] = bool(it["symbol"]) and it["symbol"] in mine
    items.sort(key=lambda i: (not i["highlight"], i["category"], i["time"] or ""), reverse=False)
    return ih.fresh(items, state), notes


ORDER = ["Bulk/Block deal", "Insider (SEBI PIT)", "SAST / Takeover", "Promoter pledge", "Shareholding pattern",
         "FII / DII flows", "Mutual fund portfolio"]


def render(items, notes, show_all=False):
    out = []
    shown = [i for i in items if i["new"] or show_all]
    for cat in ORDER:
        rows = [i for i in shown if i["category"] == cat]
        if not rows:
            continue
        rows.sort(key=lambda i: (not i["highlight"], i["time"] or ""), reverse=False)
        out.append("== %s (%d%s)" % (cat, len(rows), ", %d highlighted" % sum(r["highlight"] for r in rows)))
        for r in rows:
            mark = "*" if r["highlight"] else " "
            mark += "!" if r.get("mine") else " "
            when = (r["time"] or "")[:16]
            out.append("%s %-16s %s" % (mark, when[:10] if when.endswith("00:00") else when, r["title"]))
            out.append("      %s" % r["detail"])
            if r["link"]:
                out.append("      %s" % r["link"])
    if not shown:
        out.append("Nothing new.")
    for n in notes:
        out.append("note: %s" % n)
    out.append("* = highlighted, ! = one of your symbols. Public disclosures, read the filing before acting. "
               "Not financial advice.")
    return "\n".join(out) + "\n"


def cmd_run(args):
    client = ih.Client(offline=args.offline, record=args.record)
    state = ih.State(args.state)
    items, notes = collect(client, args, state)
    new = [i for i in items if i["new"]]
    if args.json:
        print(json.dumps({"checked": ih.now_ist().strftime("%Y-%m-%d %H:%M"), "new": len(new),
                          "items": items if args.all else new, "notes": notes}, indent=2, ensure_ascii=False, default=str))
    else:
        sys.stdout.write(render(items, notes, args.all))
    if not args.dry_run:
        ih.write_json(os.path.join(args.out, "latest.json"),
                      {"checked": ih.now_ist().strftime("%Y-%m-%d %H:%M"), "items": items, "notes": notes})
        ih.append_csv(os.path.join(args.out, "alerts.csv"), new, CSV_FIELDS)
        for i in new:
            state.mark(i["id"])
        for i in items:   # XBRL already read: do not fetch it again next run
            if i["source"] in ("NSE Insider Trading RSS", "NSE Shareholding RSS") and i["link"]:
                state.mark(("pitxbrl|" if "Insider" in i["source"] else "shpxbrl|") + i["link"])
        state.save()
    return 0 if new else 1


def cmd_amfi_links(args):
    client = ih.Client(offline=args.offline, record=args.record)
    html = client.get_text(AMFI_DIRECTORY, "amfi_portfolio_disclosure.html")
    links = amfi_directory(html)
    if args.json:
        print(json.dumps(links, indent=2))
    else:
        for l in links:
            print("%-38s %s" % (l["amc_site"], l["url"]))
        print("%d fund-house disclosure pages listed by AMFI. Download a month's portfolio file from one, then: "
              "smartmoney.py amfi --file FILE --amc NAME --month YYYY-MM" % len(links))
    return 0 if links else 1


def cmd_amfi(args):
    client = ih.Client(offline=args.offline, record=args.record)
    state = ih.State(args.state)
    notes = []
    entry = {"amc": args.amc, "month": args.month}
    entry["file" if args.file else "url"] = args.file or args.url
    items = ih.fresh(amfi_ingest(client, state, [entry], notes), state)
    sys.stdout.write(render(items, notes, args.all) if not args.json else
                     json.dumps({"items": items, "notes": notes}, indent=2, ensure_ascii=False) + "\n")
    if not args.dry_run:
        for i in items:
            if i["new"]:
                state.mark(i["id"])
        ih.append_csv(os.path.join(args.out, "alerts.csv"), [i for i in items if i["new"]], CSV_FIELDS)
        state.save()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Indian smart money & institutional tracker (trader-pelosi).")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "amfi-links", "amfi"])
    ap.add_argument("--only", help="comma-separated sources: " + ",".join(SOURCES))
    ap.add_argument("--days", type=int, default=3, help="look-back window for dated sources (3)")
    ap.add_argument("--all", action="store_true", help="show items already flagged too")
    ap.add_argument("--min-deal-cr", type=float, default=1.0, help="drop non-institutional bulk deals under this (Rs cr)")
    ap.add_argument("--min-sast-pct", type=float, default=0.1,
                    help="highlight a promoter's open-market buy from this %% of the company (0.1)")
    ap.add_argument("--fii-alert-cr", type=float, default=1000.0, help="flag an FII / DII day net of this or more (Rs cr)")
    ap.add_argument("--max-xbrl", type=int, default=25, help="XBRL files to read per feed per run (25)")
    ap.add_argument("--amfi-config", default=AMFI_CONFIG)
    ap.add_argument("--file", help="amfi: a fund house's monthly portfolio workbook (.xlsx / .xls)")
    ap.add_argument("--url", help="amfi: the workbook's URL instead")
    ap.add_argument("--amc", help="amfi: the fund house's name")
    ap.add_argument("--month", help="amfi: the portfolio month, YYYY-MM")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse, but write and remember nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-23 21:30"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers from DIR, no network")
    ap.add_argument("--record", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    ih.pin_clock(args.now)
    try:
        if args.cmd == "amfi-links":
            return cmd_amfi_links(args)
        if args.cmd == "amfi":
            if not (args.file or args.url) or not args.amc or not args.month:
                print("smartmoney.py: amfi needs --file (or --url), --amc and --month YYYY-MM", file=sys.stderr)
                return 2
            return cmd_amfi(args)
        return cmd_run(args)
    except ih.FeedError as e:
        print("smartmoney.py: %s" % e, file=sys.stderr)
        return 2
    except SystemExit as e:      # a bad --only: one plain line, exit 2
        if e.code in (None, 0):
            raise
        print(e.code if isinstance(e.code, str) else "smartmoney.py: stopped", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
