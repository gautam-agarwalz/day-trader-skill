#!/usr/bin/env python3
"""Indian Corporate Filings & Exchange Catalyst Scanner (trader-news): what NSE, BSE and SEBI
published, sorted into catalysts, from official feeds only, with no language model anywhere.

    python3 news.py                          # every source, new catalysts only -> terminal + trader/catalysts/
    python3 news.py --mine                   # only your symbols (watchlist, plan, portfolio)
    python3 news.py --tags results,buyback   # some tags
    python3 news.py --days 3 --all           # a wider window, items already flagged too
    python3 news.py --dry-run --json         # fetch and parse live, write nothing, remember nothing

Sources (each independent: one failing never stops the rest):
  nse_api      NSE corporate announcements (www.nseindia.in/api/corporate-announcements) with NSE's own
               category for each filing, and NSE circulars (www.nseindia.in/api/circulars)
  nse_rss      NSE's official RSS feeds (nsearchives.nseindia.com/content/RSS/): Online_announcements,
               Financial_Results, Board_Meetings, Corporate_action, Annual_Reports, Circulars,
               Daily_Buyback. Items the API already gave are not repeated.
  bse          BSE corporate announcements (api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w).
               BSE's firewall refuses many cloud and foreign IPs; then BSE's own RSS feeds are read
               instead (www.bseindia.com/data/xml/announcements.xml and notices.xml)
  sebi         SEBI's RSS feed (www.sebi.gov.in/sebirss.xml): circulars, orders, press releases
  surveillance NSE's ASM and GSM lists (additions, stage changes and exits since the last run) and
               the day's price-band revisions (nsearchives eq_band_changes_<DDMMYYYY>.csv)
  ir           Optional trader/ir_watchlist.json, {"SYMBOL": "https://company.com/investors"}: each
               page's links to presentations, transcripts and annual reports

Tags (keyword / regex rules on NSE's category, the subject and the text):
  results        Quarterly Results
  buyback        Buybacks & Corporate Actions (splits, bonus, dividends, record dates, rights)
  orders         Order Wins & Reg 30 Catalysts (orders, contracts, acquisitions, mergers, capacity)
  documents      Presentations, Transcripts & Annual Reports
  exchange       Exchange & Surveillance Triggers (price bands / circuits, ASM / GSM, F&O and index
                 inclusions and exclusions)
Anything else is "other" and hidden unless --all-tags.

Numbers from PDFs: for new results and buyback filings, the first --pdf-pages (2) pages of the
attached PDF are read (pypdf, pdfplumber as a fallback) and regex picks out revenue, total income,
net profit, EPS, the buyback price and size, as printed. Many results PDFs open with a cover
letter and carry the table as a scanned image: then nothing is found and the item says so.

Output: the terminal; trader/catalysts/latest.json (this run); new items appended to
trader/catalysts/alerts.csv, each with the official NSE / BSE / SEBI link. trader/state/catalysts.json
remembers every flagged item (90 days) and the surveillance lists, so nothing is flagged twice.
--offline DIR reads saved answers instead of the network (the tests use it).

Filings are leads, not advice: open the document before acting. Nothing here places an order.
"""

import argparse
import datetime as _dt
import io
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import india_http as ih  # noqa: E402
import india_results as ir  # noqa: E402
from india_catalysts import TAGS, TAG_ORDER, NOISE, NOISE_DESC, tags_for, doc_kind  # noqa: E402

SOURCES = ("nse_api", "nse_rss", "bse", "sebi", "surveillance", "upcoming", "ir")
NSE_RSS = {"announcements": "Online_announcements.xml", "results": "Financial_Results.xml",
           "board_meetings": "Board_Meetings.xml", "corporate_actions": "Corporate_action.xml",
           "annual_reports": "Annual_Reports.xml", "circulars": "Circulars.xml", "buyback": "Daily_Buyback.xml"}
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_RSS = {"announcements": "https://www.bseindia.com/data/xml/announcements.xml",
           "notices": "https://www.bseindia.com/data/xml/notices.xml"}
BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
SEBI_RSS = "https://www.sebi.gov.in/sebirss.xml"
OUT_DIR = os.path.join("trader", "catalysts")
STATE = os.path.join("trader", "state", "catalysts.json")
IR_WATCHLIST = os.path.join("trader", "ir_watchlist.json")
CSV_FIELDS = ("time", "tag", "tags", "symbol", "company", "title", "detail", "numbers", "source", "link", "id",
              "event_date", "flags")


def item(**kw):
    base = {"id": "", "source": "", "tag": "other", "tags": [], "symbol": "", "company": "", "title": "", "detail": "",
            "time": "", "link": "", "numbers": {}, "flags": []}
    base.update(kw)
    if not base["tags"]:
        base["tags"] = [base["tag"]]
    base["tag"] = base["tags"][0]
    if "documents" in base["tags"]:
        base["flags"] = list(base["flags"]) + [doc_kind("%s %s" % (base["title"], base["detail"]))]
    return base


def link_key(url):
    """The same PDF on nsearchives.nseindia.com and .in is one filing."""
    return re.sub(r"^https?://[^/]+", "", url or "").lower()


# ---------------------------------------------------------------- PDF numbers (regex, no AI)

CUR = r"(?:INR|Rs\.?|₹|Rupees)"
NUMBER = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"
UNIT = r"(?:crores?|crs?\.?|lakhs?|lacs?|millions?|mn|billions?|bn)\b"
FIELDS = [
    ("revenue", r"revenue from operations|total revenue|net sales|turnover|revenue (?:stood|grew|rose|was|of|at)"),
    ("total_income", r"total income"),
    ("net_profit", r"net profit(?: after tax)?|profit after tax|\bPAT\b|profit for the (?:period|quarter|year)"),
    ("eps", r"(?:basic|diluted)?\s*(?:\bEPS\b|earnings per (?:equity )?share)"),
]


def extract_numbers(text, kinds=("results", "buyback")):
    """Headline numbers as printed: {field: "INR 702 crores" / "1,234.56"} plus "unit" when the table says.
    Regex only: a prose sentence ("revenue ... stood at INR 702 crores") or a table line
    ("Revenue from operations  1,234.56  1,100.20 ..."; the first figure is usually the latest quarter)."""
    t = re.sub(r"[ \t]+", " ", text or "")
    out = {}
    m = re.search(r"\(\s*(?:₹|Rs\.?|INR)\s*in\s*(lakhs?|lacs?|crores?|millions?)\s*\)|"
                  r"(?:amount|figures|rupees)\s+in\s+(?:₹|Rs\.?|INR)?\s*(lakhs?|lacs?|crores?|millions?)", t, re.I)
    if m:
        out["unit"] = (m.group(1) or m.group(2)).lower()
    for field, label in (FIELDS if "results" in kinds else []):
        prose = re.search(r"(?is)(?:%s)[^.\n]{0,80}?(%s\s*%s(?:\s*%s)?)" % (label, CUR, NUMBER, UNIT), t)
        if prose:
            out[field] = re.sub(r"\s+", " ", prose.group(1)).strip()
            continue
        line = re.search(r"(?im)^\s*(?:[ivx\d]+[.)]\s*)?(?:%s)[^\n\d]{0,40}?\s(%s)(?=\s|$)" % (label, NUMBER), t)
        if line and re.search(r"\d", line.group(1)) and not re.match(r"^\(?(19|20)\d\d\)?$", line.group(1)):
            out[field] = line.group(1)
    if "buyback" not in kinds:
        return out
    price = re.search(r"(?is)(?:buy-?\s?back price|price not exceeding|at a price of|offer price)[^.]{0,40}?"
                      r"(%s\s*%s)(?:\s*/-)?(?:[^.]{0,80}?per (?:fully paid-?up )?equity share)?" % (CUR, NUMBER), t)
    if price:
        out["buyback_price"] = re.sub(r"\s+", " ", price.group(1))
    size = re.search(r"(?is)(?:maximum buy-?\s?back size|aggregate amount not exceeding|aggregate consideration not "
                     r"exceeding|buy-?\s?back size)[^.]{0,30}?(%s\s*%s(?:\s*%s)?)" % (CUR, NUMBER, UNIT), t)
    if size:
        out["buyback_size"] = re.sub(r"\s+", " ", size.group(1))
    return out


def pdf_text(data, pages=2):
    """The text of the first `pages` pages: pypdf first, pdfplumber when pypdf finds no text."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    text = ""
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        text = "\n".join((r.pages[i].extract_text() or "") for i in range(min(pages, len(r.pages))))
    except Exception:
        text = ""
    if len(text.strip()) < 40:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages[:pages])
        except Exception:
            pass
    return text


MONTH = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                     "september", "october", "november", "december"), 1)}


def period_named(text):
    """'quarter ended September 30, 2026' / '30th September, 2026' / '30.09.2026' -> 'YYYY-MM-DD' or None."""
    t = text or ""
    m = re.search(r"ended\s+(?:on\s+)?([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t, re.I)
    if m and m.group(1).lower() in MONTH:
        return "%s-%02d-%02d" % (m.group(3), MONTH[m.group(1).lower()], int(m.group(2)))
    m = re.search(r"ended\s+(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", t, re.I)
    if m and m.group(2).lower() in MONTH:
        return "%s-%02d-%02d" % (m.group(3), MONTH[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"ended\s+(?:on\s+)?(\d{1,2})[./-](\d{1,2})[./-](\d{4})", t, re.I)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1)))
    return None


def results_numbers(client, symbol, day, text=""):
    """The quarter a company filed on `day` (YYYY-MM-DD), from its integrated-filing XBRL, with the
    change on the same quarter a year earlier. Exact figures, no PDF. {} when there is none."""
    rows = ir.filings(client.nse_api(ir.INTEGRATED + "&symbol=" + symbol, "nse_results_%s.json" % ih.safe_name(symbol)))
    picks = ir.pick_quarters(rows)
    d0 = ir._date(day)
    named = period_named(text)
    filed = [f for f in picks if named and f["quarter_end"] == named]
    if not filed:
        filed = [f for f in picks if d0 and f["broadcast"] and abs((ir._date(f["broadcast"][:10]) - d0).days) <= 3]
    if not filed:
        return {"note": "the XBRL for this filing is not on NSE yet"}
    f = filed[0]

    def quarter(fl):
        x = ir.parse_results_xbrl(client.get_text(fl["xbrl"], ih.safe_name(fl["xbrl"].rsplit("/", 1)[-1])))
        return x
    cur = quarter(f)
    if not cur["quarter"]:
        return {}
    q = cur["quarter"]
    out = {"quarter_end": f["quarter_end"], "nature": cur["nature"].lower(), "revenue_cr": ir.crore(q.get("revenue")),
           "profit_cr": ir.crore(q.get("profit")), "eps": q.get("eps"), "source": "XBRL"}
    ly = [p for p in picks if p["quarter_end"] and abs((ir._date(f["quarter_end"]) - ir._date(p["quarter_end"])).days - 365) <= 10]
    if ly:
        prev = quarter(ly[0])["quarter"]
        out["revenue_yoy"] = ir.pct(q.get("revenue"), prev.get("revenue"))
        out["profit_yoy"] = ir.pct(q.get("profit"), prev.get("profit"))
    return out


def add_pdf_numbers(client, items, args, notes):
    """For new results items: the exact quarter from NSE's XBRL (the PDF only when there is no
    symbol or no XBRL). For new buyback items with a PDF: the price and size by regex."""
    if args.no_pdf:
        return
    done, found = 0, 0
    for it in items:
        if done >= args.pdf_max:
            break
        if not it.get("new") or not set(it["tags"]) & {"results", "buyback"}:
            continue
        if it["tag"] == "results" and it.get("symbol") and not it.get("event_date"):
            done += 1
            try:
                nums = results_numbers(client, it["symbol"], (it["time"] or "")[:10], it["detail"])
            except ih.FeedError as e:
                nums = {"note": "XBRL: %s" % str(e)[:100]}
            nums = {k: v for k, v in nums.items() if v is not None}
            if nums.get("revenue_cr") is not None or nums.get("profit_cr") is not None:
                it["numbers"] = nums
                found += 1
                continue
        if not it["link"].lower().endswith(".pdf"):
            continue
        done += 1
        try:
            data = client.get_bytes(it["link"], ih.safe_name(it["link"].rsplit("/", 1)[-1]), max_bytes=15 * 1048576)
        except ih.FeedError as e:
            it["numbers"] = {"note": str(e)[:120]}
            continue
        text = pdf_text(data, args.pdf_pages)
        nums = extract_numbers(text, [t for t in it["tags"] if t in ("results", "buyback")])
        if not text.strip():
            nums["note"] = "no text in the first %d page(s): likely a scanned image" % args.pdf_pages
        elif not [k for k in nums if k != "unit"]:
            nums["note"] = "no headline numbers in the first %d page(s) (often a cover letter)" % args.pdf_pages
        else:
            found += 1
        it["numbers"] = nums
    if done:
        notes.append("numbers: read %d results / buyback filing(s) (XBRL first, PDF otherwise), numbers found in %d"
                     % (done, found))


# ---------------------------------------------------------------- NSE

def nse_announcement_items(rows):
    out = []
    for a in rows or []:
        desc, text = a.get("desc") or "", a.get("attchmntText") or ""
        if desc in NOISE_DESC or NOISE.search(text):
            continue
        out.append(item(id="nse|" + (link_key(a.get("attchmntFile")) or str(a.get("seq_id"))), source="NSE announcements",
                        tags=tags_for(text, desc, company=a.get("sm_name") or ""), symbol=a.get("symbol") or "", company=a.get("sm_name") or "",
                        title="%s: %s" % (a.get("symbol") or a.get("sm_name"), desc), detail=text[:400],
                        time=ih.parse_when(a.get("an_dt") or a.get("sort_date")), link=a.get("attchmntFile") or "",
                        flags=[desc] if desc else []))
    return out


def nse_circular_items(doc):
    out = []
    for c in (doc or {}).get("data") or []:
        sub = c.get("sub") or ""
        tags = tags_for(sub)
        if "exchange" not in tags:
            continue           # only circulars that move a stock: bands, surveillance, F&O, indices
        out.append(item(id="nse|" + (link_key(c.get("circFilelink")) or str(c.get("circDisplayNo"))), source="NSE circulars",
                        tags=["exchange"], title="NSE circular %s: %s" % (c.get("circDisplayNo"), sub),
                        detail="%s / %s" % (c.get("circDepartment") or "", c.get("circCategory") or ""),
                        time=ih.parse_when(c.get("cirDate")), link=c.get("circFilelink") or "",
                        flags=[c.get("circDepartment") or "circular"]))
    return out


def nse_rss_items(feed_name, entries, names_to_symbol=None):
    out = []
    for e in entries:
        title, summary = e["title"], re.sub(r"\s+", " ", e["summary"])
        if NOISE.search(summary):
            continue
        company = "" if feed_name == "circulars" else re.sub(r"\s+-\s+Ex-Date.*$", "", title).strip()
        tags = tags_for("%s %s" % (title, summary), feed=feed_name, company=company)
        if feed_name == "circulars" and "exchange" not in tags:
            continue
        sym = (names_to_symbol or {}).get(company.lower(), "")
        out.append(item(id="nse|" + (link_key(e["link"]) if e["link"].lower().endswith((".pdf", ".xml", ".zip")) else
                                     "%s|%s|%s" % (feed_name, title, e["published"])),
                        source="NSE RSS %s" % feed_name.replace("_", " "), tags=tags, symbol=sym, company=company,
                        title="%s: %s" % (sym or company or "NSE", (summary.split("|")[0] if summary else title)[:160]),
                        detail=summary[:400], time=e["time"], link=e["link"], flags=[feed_name]))
    return out


# ---------------------------------------------------------------- BSE and SEBI

def bse_api_items(doc):
    """BSE's AnnSubCategoryGetData answer ({"Table": [...]}) -> items."""
    out = []
    for r in (doc or {}).get("Table") or []:
        head = r.get("HEADLINE") or r.get("NEWSSUB") or ""
        cat = r.get("SUBCATNAME") or r.get("CATEGORYNAME") or ""
        att = r.get("ATTACHMENTNAME") or ""
        if NOISE.search("%s %s %s" % (head, cat, r.get("NEWSSUB") or "")):
            continue
        out.append(item(id="bse|" + str(r.get("NEWSID") or att), source="BSE announcements",
                        tags=tags_for("%s %s" % (r.get("NEWSSUB") or "", head), cat, company=r.get("SLONGNAME") or ""), symbol=str(r.get("SCRIP_CD") or ""),
                        company=r.get("SLONGNAME") or "", title="%s: %s" % (r.get("SLONGNAME") or r.get("SCRIP_CD"),
                                                                            (r.get("NEWSSUB") or head)[:160]),
                        detail=head[:400], time=ih.parse_when(r.get("NEWS_DT") or r.get("DT_TM")),
                        link=(BSE_PDF + att) if att else "https://www.bseindia.com/corporates/ann.html", flags=[cat] if cat else []))
    return out


def rss_items(entries, source, prefix, exchange_only=False):
    out = []
    for e in entries:
        if NOISE.search("%s %s" % (e["title"], e["summary"])):
            continue
        company = re.split(r"\s*[(:]", e["title"])[0] if not exchange_only else ""
        tags = tags_for("%s %s" % (e["title"], e["summary"]), company=company)
        if exchange_only and "exchange" not in tags:
            continue
        out.append(item(id="%s|%s" % (prefix, e["link"] or e["title"]), source=source, tags=tags,
                        title=e["title"][:200], detail=re.sub(r"\s+", " ", e["summary"])[:400], time=e["time"],
                        link=e["link"]))
    return out


def collect_bse(client, days, notes):
    to = ih.now_ist().date()
    params = {"pageno": 1, "strCat": -1, "strPrevDate": (to - _dt.timedelta(days=days)).strftime("%Y%m%d"),
              "strScrip": "", "strSearch": "P", "strToDate": to.strftime("%Y%m%d"), "strType": "C", "subcategory": -1}
    hdrs = {"Accept": ih.JSON_ACCEPT, "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
    try:
        body = client.get_text(BSE_API, "bse_announcements.json", headers=hdrs, params=params, site="bse")
        return bse_api_items(json.loads(body))
    except (ih.FeedError, ValueError) as e:
        notes.append("bse: the announcements API did not answer (%s); reading BSE's RSS feeds instead" % str(e)[:120])
    items = []
    for name, url in BSE_RSS.items():
        try:
            entries = ih.parse_rss(client.get_bytes(url, "bse_%s.xml" % name))
            items += rss_items(entries, "BSE RSS %s" % name, "bse" + name, exchange_only=(name == "notices"))
            if not entries:
                notes.append("bse: the %s RSS feed is empty right now" % name)
        except ih.FeedError as e:
            notes.append("bse %s RSS: %s" % (name, e))
    return items


# ---------------------------------------------------------------- surveillance

def surveillance_snapshot(asm, gsm):
    snap = {}
    for term in ("longterm", "shortterm"):
        for r in ((asm or {}).get(term) or {}).get("data") or []:
            if r.get("symbol"):
                snap["%s|%s" % ("LT-ASM" if term == "longterm" else "ST-ASM", r["symbol"])] = {
                    "stage": r.get("asmSurvIndicator") or r.get("survCode"), "name": r.get("companyName")}
    for r in gsm or []:
        sym = r.get("symbol") or r.get("isin")
        if sym:
            snap["GSM|%s" % sym] = {"stage": r.get("gsmStage") or r.get("survCode"), "name": r.get("companyName")}
    return snap


def surveillance_changes(prev, cur, day):
    out = []
    for key in sorted(set(prev or {}) | set(cur)):
        kind, sym = key.split("|", 1)
        p, c = (prev or {}).get(key), cur.get(key)
        if p and c and p["stage"] == c["stage"]:
            continue
        if c and not p:
            what = "added to %s %s" % (kind, c["stage"])
        elif p and not c:
            what = "exited %s (was %s)" % (kind, p["stage"])
        else:
            what = "%s stage %s -> %s" % (kind, p["stage"], c["stage"])
        name = (c or p).get("name") or ""
        out.append(item(id="surv|%s|%s|%s" % (key, day, what), source="NSE surveillance lists", tags=["exchange"],
                        symbol=sym, company=name, title="%s: %s" % (sym, what), time=day + " 00:00",
                        detail="NSE %s list" % kind, link=ih.NSE + "/reports/" + ("gsm" if kind == "GSM" else "asm"),
                        flags=[kind]))
    return out


def price_band_items(text, day):
    out = []
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines or "Symbol" not in lines[0]:
        return out
    head = [h.strip() for h in lines[0].split(",")]
    for ln in lines[1:]:
        r = dict(zip(head, [c.strip() for c in ln.split(",")]))
        if not r.get("Symbol"):
            continue
        frm, to = ih.num(r.get("From")), ih.num(r.get("To"))
        what = "price band %s%% -> %s%%" % (r.get("From"), r.get("To"))
        if frm is not None and to is not None:
            what += " (tightened)" if to < frm else " (widened)"
        out.append(item(id="band|%s|%s" % (day, r["Symbol"]), source="NSE price band changes", tags=["exchange"],
                        symbol=r["Symbol"], company=r.get("Security Name") or "", title="%s: %s" % (r["Symbol"], what),
                        detail="series %s, effective from the next session" % r.get("Series"), time=day + " 00:00",
                        link="%s/content/equities/eq_band_changes_%s.csv" % (ih.NSE_ARCHIVE,
                                                                             _dt.datetime.strptime(day, "%Y-%m-%d").strftime("%d%m%Y")),
                        flags=["circuit"]))
    return out


def collect_surveillance(client, state, notes):
    items = []
    today = ih.now_ist().strftime("%Y-%m-%d")
    try:
        cur = surveillance_snapshot(client.nse_api("/api/reportASM", "nse_asm.json"),
                                    client.nse_api("/api/reportGSM", "nse_gsm.json"))
        prev = state.snapshot("surveillance")
        state.set_snapshot("surveillance", cur)
        if prev is None:
            notes.append("surveillance: first run, %d ASM / GSM entries saved as the baseline (additions and exits "
                         "show from the next run)" % len(cur))
        else:
            items += surveillance_changes(prev, cur, today)
    except ih.FeedError as e:
        notes.append("surveillance ASM / GSM: %s" % e)
    day = ih.now_ist().date()
    for back in range(0, 7):
        d = day - _dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        name = "eq_band_changes_%s.csv" % d.strftime("%d%m%Y")
        try:
            text = client.get_text("%s/content/equities/%s" % (ih.NSE_ARCHIVE, name), name)
        except ih.FeedError:
            continue
        items += price_band_items(text, d.isoformat())
        break
    else:
        notes.append("surveillance: no price-band file in the last week")
    return items


# ---------------------------------------------------------------- company IR pages

IR_LINK = re.compile(r"presentation|transcript|annual[ -]?report|con(?:ference)?[ -]?call|earnings[ -]?call|"
                     r"investor[ -]?(?:meet|day)|analyst", re.I)


def ir_items(symbol, page_url, html):
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href, text = urljoin(page_url, a["href"]), " ".join(a.get_text(" ").split())
        if href in seen or not (IR_LINK.search(text) or IR_LINK.search(href)):
            continue
        seen.add(href)
        kind = ("transcript" if re.search(r"transcript", text + href, re.I) else
                "annual report" if re.search(r"annual", text + href, re.I) else "presentation")
        out.append(item(id="ir|" + href, source="company IR page", tags=["documents"], symbol=symbol,
                        title="%s: %s" % (symbol, text[:150] or href.rsplit("/", 1)[-1]), detail=kind,
                        link=href, flags=[kind]))
    return out


def upcoming_items(rows, today):
    """NSE's event calendar -> scheduled board meetings from today on, tagged by purpose (results,
    buyback, fundraise...): the early signals."""
    out = []
    for m in ir.board_meetings(rows):
        if m["date"] < today:
            continue
        text = "%s %s" % (m["purpose"], m["desc"])
        tags = tags_for(text)
        if re.search(r"fund ?rais|preferential|qualified institutions|\bQIP\b|rights", text, re.I) and "orders" not in tags:
            tags = [t for t in tags if t != "other"] + ["orders"]
        out.append(item(id="upcoming|%s|%s|%s" % (m["symbol"], m["date"], m["purpose"]), source="NSE event calendar",
                        tags=tags, symbol=m["symbol"], title="%s: board meeting on %s to consider %s" % (
                            m["symbol"], m["date"], (m["purpose"] or "business").lower()),
                        detail=m["desc"][:300], time=today + " 00:00", event_date=m["date"],
                        link=ih.NSE + "/companies-listing/corporate-filings-event-calendar", flags=["scheduled " + m["date"]]))
    return out


def collect_ir(client, path, notes):
    cfg = ih.load_json(path)
    if not cfg:
        return []
    items = []
    for sym, urls in cfg.items():
        for u in ([urls] if isinstance(urls, str) else urls or []):
            try:
                items += ir_items(sym.upper(), u, client.get_text(u, ih.safe_name(u)))
            except ih.FeedError as e:
                notes.append("ir %s: %s" % (sym, e))
    return items


# ---------------------------------------------------------------- the run

def collect(client, args, state):
    only = set(args.only.split(",")) if args.only else set(SOURCES)
    bad = only - set(SOURCES)
    if bad:
        raise SystemExit("news.py: unknown source(s) %s; choose from %s" % (", ".join(sorted(bad)), ", ".join(SOURCES)))
    notes, items = [], []

    def run(name, fn):
        if name in only:
            try:
                items.extend(fn())
            except ih.FeedError as e:
                notes.append("%s: %s" % (name, e))
            except Exception as e:  # one broken source never stops the others
                notes.append("%s: unexpected %s: %s" % (name, type(e).__name__, str(e)[:200]))

    names = {}

    def nse_api():
        to = ih.now_ist().date()
        frm = to - _dt.timedelta(days=args.days)
        rows = client.nse_api("/api/corporate-announcements?index=equities&from_date=%s&to_date=%s"
                              % (frm.strftime("%d-%m-%Y"), to.strftime("%d-%m-%Y")), "nse_announcements.json") or []
        for r in rows:
            if r.get("sm_name") and r.get("symbol"):
                names[r["sm_name"].strip().lower()] = r["symbol"]
        out = nse_announcement_items(rows)
        try:
            out += nse_circular_items(client.nse_api("/api/circulars", "nse_circulars.json"))
        except ih.FeedError as e:
            notes.append("nse circulars: %s" % e)
        return out
    run("nse_api", nse_api)

    def nse_rss():
        out = []
        for feed, fname in NSE_RSS.items():
            try:
                entries = ih.parse_rss(client.get_bytes("%s/content/RSS/%s" % (ih.NSE_ARCHIVE, fname), "rss_" + fname))
                out += nse_rss_items(feed, entries, names)
            except ih.FeedError as e:
                notes.append("nse rss %s: %s" % (feed, e))
        return out
    run("nse_rss", nse_rss)
    run("bse", lambda: collect_bse(client, args.days, notes))

    def sebi():
        entries = ih.parse_rss(client.get_bytes(SEBI_RSS, "sebirss.xml"))
        if not entries:
            notes.append("sebi: the RSS feed is empty right now")
        return rss_items(entries, "SEBI RSS", "sebi")
    run("sebi", sebi)
    run("surveillance", lambda: collect_surveillance(client, state, notes))
    run("upcoming", lambda: upcoming_items(client.nse_api(ir.CALENDAR, "nse_event_calendar.json"),
                                            ih.now_ist().strftime("%Y-%m-%d")))
    run("ir", lambda: collect_ir(client, args.ir_watchlist, notes))

    # one filing, one item (the API and the RSS carry the same PDFs); the API row wins
    uniq, seen = [], set()
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    since = (ih.now_ist() - _dt.timedelta(days=args.days + 1)).strftime("%Y-%m-%d")
    uniq = [i for i in uniq if not i["time"] or i["time"] >= since or i["source"].startswith(("company IR",))]
    mine = ih.my_symbols()
    for it in uniq:
        it["mine"] = bool(it["symbol"]) and it["symbol"] in mine
    return ih.fresh(uniq, state), notes


def select(items, args):
    want = set(args.tags.split(",")) if args.tags else set(TAGS) - (set() if args.all_tags else {"other"})
    bad = want - set(TAGS)
    if bad:
        raise SystemExit("news.py: unknown tag(s) %s; choose from %s" % (", ".join(sorted(bad)), ", ".join(TAGS)))
    out = [i for i in items if set(i["tags"]) & want and (i["new"] or args.all)]
    if args.mine:
        out = [i for i in out if i["mine"]]
    return out


def render(shown, notes):
    out = []
    for tag in TAG_ORDER:
        rows = [i for i in shown if i["tag"] == tag]
        if not rows:
            continue
        rows.sort(key=lambda i: i["time"] or "", reverse=True)
        out.append("== %s (%d)" % (TAGS[tag], len(rows)))
        for r in rows:
            when = (r["time"] or "")[:16]
            out.append("%s %-16s %s" % ("!" if r.get("mine") else " ", when[:10] if when.endswith("00:00") else when,
                                        r["title"][:150]))
            extra = [t for t in r["tags"][1:]]
            line = "      " + (("also: %s  " % ", ".join(extra)) if extra else "") + (r["detail"][:180] if r["detail"] else "")
            if line.strip():
                out.append(line)
            nums = {k: v for k, v in (r.get("numbers") or {}).items()}
            if nums:
                src = "NSE XBRL, Rs crore" if nums.get("source") == "XBRL" else "first PDF pages, as printed"
                out.append("      numbers (%s): " % src +
                           ", ".join("%s %s" % (k.replace("_", " "), v) for k, v in nums.items() if k != "source"))
            if r["link"]:
                out.append("      %s" % r["link"])
    if not shown:
        out.append("Nothing new.")
    for n in notes:
        out.append("note: %s" % n)
    out.append("! = one of your symbols. Filings are leads, not advice: open the document before acting.")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Indian corporate filings & exchange catalyst scanner (trader-news).")
    ap.add_argument("--only", help="comma-separated sources: " + ",".join(SOURCES))
    ap.add_argument("--tags", help="comma-separated tags: " + ",".join(TAGS))
    ap.add_argument("--all-tags", action="store_true", help="include untagged filings (other)")
    ap.add_argument("--mine", action="store_true", help="only your symbols (watchlist, plan, portfolio)")
    ap.add_argument("--days", type=int, default=1, help="look-back window in days (1)")
    ap.add_argument("--all", action="store_true", help="show items already flagged too")
    ap.add_argument("--pdf-pages", type=int, default=2, help="PDF pages to read for numbers (2)")
    ap.add_argument("--pdf-max", type=int, default=8, help="results / buyback PDFs to read per run (8)")
    ap.add_argument("--no-pdf", action="store_true", help="do not download PDFs")
    ap.add_argument("--ir-watchlist", default=IR_WATCHLIST)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="fetch and parse, but write and remember nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help='pin the clock, India time: "2026-11-23 21:30"')
    ap.add_argument("--offline", metavar="DIR", help="read saved answers from DIR, no network")
    ap.add_argument("--record", metavar="DIR", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    ih.pin_clock(args.now)
    client = ih.Client(offline=args.offline, record=args.record)
    state = ih.State(args.state)
    try:
        items, notes = collect(client, args, state)
        shown = select(items, args)
    except ih.FeedError as e:
        print("news.py: %s" % e, file=sys.stderr)
        return 2
    except SystemExit as e:      # a bad --only / --tags: one plain line, exit 2
        if e.code in (None, 0):
            raise
        print(e.code if isinstance(e.code, str) else "news.py: stopped", file=sys.stderr)
        return 2
    add_pdf_numbers(client, shown, args, notes)
    if args.json:
        print(json.dumps({"checked": ih.now_ist().strftime("%Y-%m-%d %H:%M"), "items": shown, "notes": notes,
                          "counts": {t: sum(1 for i in shown if i["tag"] == t) for t in TAGS}},
                         indent=2, ensure_ascii=False, default=str))
    else:
        sys.stdout.write(render(shown, notes))
    if not args.dry_run:
        # the other tools read latest.json: it keeps every catalyst of the look-back window (new or already
        # seen, whatever --mine / --tags showed), with "new" as a flag; the CSV gets every new one
        window = [i for i in items if set(i["tags"]) - ({"other"} if not args.all_tags else set())]
        new = [i for i in items if i["new"]]
        ih.write_json(os.path.join(args.out, "latest.json"),
                      {"checked": ih.now_ist().strftime("%Y-%m-%d %H:%M"), "items": window, "notes": notes})
        ih.append_csv(os.path.join(args.out, "alerts.csv"), [i for i in window if i["new"]], CSV_FIELDS)
        for i in new:
            state.mark(i["id"])
        state.save()
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
