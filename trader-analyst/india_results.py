"""NSE results data, shared by trader-analyst (facts.py) and trader-news (news.py). The same copy
lives in both folders (a repo test keeps them identical). No language model: JSON and XBRL parsing.

Where the numbers come from (all official NSE):
  /api/integrated-filing-results?...type=Integrated Filing- Financials[&symbol=X]
        every results filing under SEBI's integrated filing (from early 2025), with its XBRL
  the XBRL itself (nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_..._WEB.xml)
        revenue, other income, total income, expenses, finance costs, depreciation, profit before
        tax, profit, basic and diluted EPS, for the quarter and the year to date, in rupees
  /api/results-comparision?symbol=X
        NSE's older results feed (quarters up to Dec 2024, standalone, in lakhs)
  /api/corporate-board-meetings?index=equities&symbol=X and /api/event-calendar?index=equities
        board meetings: when the next results (or a buyback, a fundraise...) will be considered
"""

import datetime as _dt
import html
import re

INTEGRATED = "/api/integrated-filing-results?index=equities&type=Integrated%20Filing-%20Financials"
COMPARISON = "/api/results-comparision?symbol=%s"
BOARD = "/api/corporate-board-meetings?index=equities&symbol=%s"
CALENDAR = "/api/event-calendar?index=equities"
CRORE = 1e7
LAKH = 1e5

# XBRL element -> field, first match wins (banks and NBFCs file some lines under other names)
XBRL_FIELDS = [
    ("revenue", ("RevenueFromOperations", "InterestEarned", "TotalRevenueFromOperations")),
    ("other_income", ("OtherIncome",)),
    ("total_income", ("Income", "TotalIncome")),
    ("expenses", ("Expenses", "TotalExpenses")),
    ("finance_costs", ("FinanceCosts", "InterestExpended")),
    ("depreciation", ("DepreciationDepletionAndAmortisationExpense",)),
    ("pbt", ("ProfitBeforeTax", "ProfitLossBeforeTax")),
    ("profit", ("ProfitLossForPeriod", "ProfitLossForThePeriod", "NetProfitLossForThePeriod")),
    ("eps", ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
             "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShareAfterExtraordinaryItems")),
    ("eps_diluted", ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                     "DilutedEarningsLossPerShareFromContinuingOperations")),
]


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _date(text):
    """'30-SEP-2026', '2026-09-30', '08-Oct-2026 22:07:33' -> date, or None."""
    t = str(text or "").strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d-%b-%Y", 11)):
        try:
            return _dt.datetime.strptime(t[:n].title() if "b" in fmt else t[:n], fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- the XBRL

def parse_results_xbrl(text):
    """An integrated-filing XBRL -> {symbol, company, nature, period_start, period_end, currency,
    quarter: {field: rupees}, ytd: {...}}. Segment contexts are ignored; a ~3-month context is the
    quarter, a longer one the year to date."""
    periods = {}
    for cid, body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', text, re.S):
        if "explicitMember" in body or "typedMember" in body:
            continue                                   # a segment or a category, not the company total
        s = re.search(r"<xbrli:startDate>([^<]+)", body)
        e = re.search(r"<xbrli:endDate>([^<]+)", body)
        if s and e:
            periods[cid] = (_date(s.group(1)), _date(e.group(1)))
    facts = {}
    meta = {}
    for name, ctx, value in re.findall(r'<[\w-]+:(\w+)\s+contextRef="([^"]+)"[^>]*>([^<]*)<', text):
        value = html.unescape(value.strip())
        if name in ("Symbol", "NameOfTheCompany", "NatureOfReportStandaloneConsolidated",
                    "DescriptionOfPresentationCurrency") and value and name not in meta:
            meta[name] = value
        if ctx in periods:
            facts.setdefault(ctx, {})[name] = value
    out = {"symbol": meta.get("Symbol", ""), "company": meta.get("NameOfTheCompany", ""),
           "nature": meta.get("NatureOfReportStandaloneConsolidated", ""),
           "currency": meta.get("DescriptionOfPresentationCurrency", "INR"),
           "period_start": None, "period_end": None, "quarter": {}, "ytd": {}}
    best = {}
    for ctx, (start, end) in periods.items():
        if not (start and end) or ctx not in facts:
            continue
        days = (end - start).days + 1
        kind = "quarter" if 80 <= days <= 100 else "ytd" if days > 100 else None
        if not kind:
            continue
        vals = {}
        for field, names in XBRL_FIELDS:
            for n in names:
                v = _num(facts[ctx].get(n))
                if v is not None:
                    vals[field] = v
                    break
        if not vals:
            continue
        key = (kind, end)
        if key not in best or len(vals) > len(best[key][1]):
            best[key] = ((start, end), vals)
    quarters = sorted((k[1], v) for k, v in best.items() if k[0] == "quarter")
    ytds = sorted((k[1], v) for k, v in best.items() if k[0] == "ytd")
    if quarters:
        (out["period_start"], out["period_end"]), out["quarter"] = quarters[-1][1]
    if ytds:
        out["ytd"] = ytds[-1][1][1]
        out["ytd_start"], out["ytd_end"] = ytds[-1][1][0]
    return out


# ---------------------------------------------------------------- the filing lists

def filings(doc):
    """The integrated-filing-results answer -> [{symbol, company, quarter_end, broadcast, nature,
    audited, revised, xbrl, ixbrl}] (broadcast: 'YYYY-MM-DD HH:MM')."""
    out = []
    for r in (doc or {}).get("data") or []:
        qe = _date(r.get("qe_Date"))
        b = str(r.get("broadcast_Date") or "")
        bd = _date(b)
        out.append({"symbol": r.get("symbol") or "", "company": r.get("smName") or r.get("cmName") or "",
                    "quarter_end": qe.isoformat() if qe else "",
                    "broadcast": ("%s %s" % (bd.isoformat(), b[12:17])) if bd else "",
                    "nature": r.get("consolidated") or "", "audited": r.get("audited") or "",
                    "revised": (r.get("type_Sub") or "").lower().startswith("revis"),
                    "xbrl": r.get("xbrl") or "", "ixbrl": r.get("ixbrl") or ""})
    return out


def pick_quarters(rows, nature="Consolidated"):
    """One filing a quarter: the wanted nature (else the other one), the latest broadcast (a
    revision replaces the original). Newest quarter first."""
    by = {}
    for r in rows:
        if not r["quarter_end"] or not r["xbrl"] or not r["xbrl"].lower().endswith(".xml"):
            continue
        rank = (r["nature"].lower() == nature.lower(), r["broadcast"])
        if r["quarter_end"] not in by or rank > by[r["quarter_end"]][0]:
            by[r["quarter_end"]] = (rank, r)
    return [by[q][1] for q in sorted(by, reverse=True)]


def comparison_quarters(doc):
    """NSE's older results-comparison feed (standalone, in lakhs) -> quarters in rupees."""
    out = []
    for r in (doc or {}).get("resCmpData") or []:
        start, end = _date(r.get("re_from_dt")), _date(r.get("re_to_dt"))
        if not end:
            continue
        lakhs = lambda k: (_num(r.get(k)) * LAKH) if _num(r.get(k)) is not None else None  # noqa: E731
        eps = _num(r.get("re_basic_eps_for_cont_dic_opr")) or _num(r.get("re_basic_eps"))
        q = {k: v for k, v in (("revenue", lakhs("re_net_sale")), ("total_income", lakhs("re_total_inc")),
                               ("other_income", lakhs("re_oth_inc_new")), ("pbt", lakhs("re_pro_loss_bef_tax")),
                               ("profit", lakhs("re_net_profit")), ("finance_costs", lakhs("re_int_new")),
                               ("depreciation", lakhs("re_depr_und_exp")), ("eps", eps)) if v is not None}
        out.append({"period_start": start, "period_end": end, "quarter": q, "nature": "Standalone",
                    "source": "NSE results comparison"})
    return out


def board_meetings(doc):
    """corporate-board-meetings / event-calendar answers -> [{symbol, date, purpose, desc}]."""
    out = []
    for r in doc or []:
        d = _date(r.get("bm_date") or r.get("date"))
        if not d:
            continue
        out.append({"symbol": r.get("bm_symbol") or r.get("symbol") or "", "date": d.isoformat(),
                    "purpose": r.get("purpose") or r.get("bm_purpose") or "",
                    "desc": " ".join(str(r.get("bm_desc") or "").split())})
    return out


RESULTS_MEETING = re.compile(r"financial results?|quarterly results|results for the (?:quarter|period|half)", re.I)


def next_results_meeting(meetings, today):
    """The first board meeting on or after `today` that will consider financial results."""
    soon = sorted((m for m in meetings if m["date"] >= today and RESULTS_MEETING.search(m["purpose"] + " " + m["desc"])),
                  key=lambda m: m["date"])
    return soon[0] if soon else None


# ---------------------------------------------------------------- arithmetic

def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100 * (1 if b > 0 else -1), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


def same_quarter_last_year(quarters, end):
    """The quarter ending about a year before `end` (10 days either way)."""
    for q in quarters:
        if q["period_end"] and abs((end - q["period_end"]).days - 365) <= 10:
            return q
    return None


def crore(x):
    return None if x is None else round(x / CRORE, 2)
