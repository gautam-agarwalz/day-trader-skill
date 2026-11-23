"""Catalyst rules shared by trader-news (news.py) and trader-timemachine (events.py): the five
catalyst tags, NSE's own announcement categories (old and new names), the noise filter, and the
finer event types the event study learns from, each with a direction. Regex only, no language
model. The same copy lives in both folders (a repo test keeps them identical).
"""

import re

TAGS = {"results": "Quarterly Results", "buyback": "Buybacks & Corporate Actions",
        "orders": "Order Wins & Reg 30 Catalysts", "documents": "Presentations, Transcripts & Annual Reports",
        "exchange": "Exchange & Surveillance Triggers", "other": "Other"}
TAG_ORDER = ["exchange", "buyback", "results", "orders", "documents", "other"]

RULES = {
    "exchange": re.compile(r"price band|circuit (?:filter|limit)|\bASM\b|\bGSM\b|\bESM\b|additional surveillance|"
                           r"graded surveillance|surveillance measure|futures (?:&|and) options|\bF&O\b|"
                           r"derivatives? (?:contracts?|segment)|introduction of (?:\w+ )*(?:securities|stocks|"
                           r"contracts)|exclusion of|inclusion (?:of|in)|index (?:rebalanc|review|replacement|"
                           r"reconstitut)|rebalanc|\bMWPL\b|ban period|trade[- ]for[- ]trade|T2T", re.I),
    "buyback": re.compile(r"buy[- ]?back|stock split|sub-?division|split of (?:equity )?shares|\bbonus\b|dividend|"
                          r"record date|rights issue|\bex[- ]date|consolidation of shares|reduction of capital", re.I),
    "results": re.compile(r"financial results?|quarterly results|un-?audited|audited (?:standalone|consolidated|"
                          r"financial)|results for the (?:quarter|period|half)|integrated filing.{0,5}financial", re.I),
    "orders": re.compile(r"bagging|receiving of orders|order(?:s)? (?:worth|valued|of|from|received|win|inflow)|"
                         r"letter of (?:award|intent)|\bLoA\b|\bLoI\b|contract (?:worth|awarded|from|for|of)|"
                         r"award(?:ed)? (?:of )?(?:a )?(?:contract|order|project)|acquisition|amalgamation|merger|"
                         r"joint venture|\bMoU\b|memorandum of understanding|capacity expansion|commercial "
                         r"production|commissioning|strategic partnership|scheme of arrangement|demerger", re.I),
    "documents": re.compile(r"investor presentation|earnings (?:call|presentation)|con(?:ference)?\.? ?call|"
                            r"transcript|annual report|analysts?/institutional investor meet|analyst meet", re.I),
}
NSE_DESC = {  # NSE's own announcement categories -> tag (checked before the text rules)
    "Bagging/Receiving of orders/contracts": "orders", "Acquisition": "orders", "Amalgamation/Merger": "orders",
    "Agreements": "orders", "Joint Venture": "orders", "Scheme of Arrangement": "orders",
    "Investor Presentation": "documents", "Analysts/Institutional Investor Meet/Con. Call Updates": "documents",
    "Annual Report": "documents", "Integrated Filing- Financial": "results", "Financial Result Updates": "results",
    "Clarification - Financial Results": "results", "Buyback": "buyback", "Dividend": "buyback",
    "Record Date": "buyback", "Bonus": "buyback", "Rights Issue": "buyback", "Stock Split": "buyback",
}
NOISE = re.compile(r"newspaper (?:publication|advertisement)|trading window|loss of share certificate|"
                   r"duplicate share certificate|issue of duplicate|monitoring agency report", re.I)
NOISE_DESC = {"Copy of Newspaper Publication", "Trading Window", "Loss of Share Certificates",
              "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
              "Duplicate Share Certificate", "Monitoring Agency Report"}


def tags_for(text, desc="", feed="", company=""):
    """Every tag a filing earns, in priority order (first = primary). The company's own name is taken
    out of the text first, so "GSM Foils Limited" is not a surveillance trigger."""
    if company and len(company) > 3:
        text = re.sub(re.escape(company), " ", text, flags=re.I)
    found = []
    if desc in NSE_DESC:
        found.append(NSE_DESC[desc])
    if feed in ("corporate_actions", "buyback"):
        found.append("buyback")
    if feed == "results":
        found.append("results")
    if feed == "annual_reports":
        found.append("documents")
    hay = "%s %s" % (desc, text)
    for tag in TAG_ORDER:
        if tag in RULES and RULES[tag].search(hay):
            found.append(tag)
    out = []
    for t in TAG_ORDER:
        if t in found and t not in out:
            out.append(t)
    return out or ["other"]


def doc_kind(text):
    if re.search(r"transcript", text, re.I):
        return "transcript"
    if re.search(r"presentation", text, re.I):
        return "presentation"
    if re.search(r"annual report", text, re.I):
        return "annual report"
    if re.search(r"audio|recording", text, re.I):
        return "call recording"
    return "meeting / call notice"


# ---------------------------------------------------------------- event types (events.py)

# (type, direction, rule). Checked in order on "<NSE category> <subject / text>"; the first match wins.
# direction: "up" / "down" when filings of this type usually mean good / bad news, "?" when only the
# price reaction can tell (results, management changes, acquisitions).
EVENT_RULES = [
    ("price_query", "?", re.compile(r"^(?:spurt in volume|price movement|reply to clarification sought|"
                                    r"clarification sought)\b|clarification (?:sought|on|regarding) (?:the )?"
                                    r"(?:price|volume) movement|significant (?:increase|movement) in (?:the )?"
                                    r"(?:price|volume)", re.I)),
    ("media_clarification", "?", re.compile(r"^news verification\b|clarification (?:on|regarding|to|with "
                                            r"reference to) (?:the )?(?:news|media|article|report)|"
                                            r"(?:news|media) (?:item|report|article)", re.I)),
    ("board_meeting_buyback", "up", re.compile(r"board meeting.{0,200}(?:to (?:inter[- ]alia,? )?consider|will consider|for considering).{0,120}buy[- ]?back", re.I | re.S)),
    ("board_meeting_bonus_split", "up", re.compile(r"board meeting.{0,200}(?:to (?:inter[- ]alia,? )?consider|will consider|for considering).{0,120}"
                                                   r"(?:bonus|split|sub-?division)", re.I | re.S)),
    ("board_meeting_fundraise", "?", re.compile(r"board meeting.{0,200}(?:to (?:inter[- ]alia,? )?consider|will consider|for considering).{0,120}"
                                                r"(?:fund ?rais|preferential|qualified institution|\bQIP\b|rights issue)",
                                                re.I | re.S)),
    ("board_meeting_results", "?", re.compile(r"board meeting.{0,200}(?:to (?:inter[- ]alia,? )?consider|will consider|for considering).{0,120}"
                                              r"financial results", re.I | re.S)),
    ("buyback", "up", re.compile(r"buy[- ]?back", re.I)),
    ("bonus_split", "up", re.compile(r"\bbonus\b|stock split|sub-?division|split of (?:equity )?shares", re.I)),
    ("auditor_resignation", "down", re.compile(r"resignation of (?:the )?(?:statutory )?auditor", re.I)),
    ("default", "down", re.compile(r"^defaults? on payment|default (?:in|on) (?:the )?(?:payment|repayment)|"
                                   r"corporate insolvency|insolvency (?:and bankruptcy|resolution)|\bIBC\b", re.I)),
    ("rating_down", "down", re.compile(r"credit rating.{0,200}(?:downgrad|revised (?:from .{0,40} )?to .{0,20}negative|"
                                       r"watch with negative)", re.I | re.S)),
    ("rating_up", "up", re.compile(r"credit rating.{0,200}(?:upgrad|revised .{0,40}positive|outlook .{0,20}positive)",
                                   re.I | re.S)),
    ("ceo_change", "?", re.compile(r"(?:appoint|re-?appoint|resign|cessation|step(?:s|ped)? down|reinstat).{0,160}"
                                   r"(?:chief executive|\bCEO\b|managing director|\bMD\b)|(?:chief executive|\bCEO\b|"
                                   r"managing director|\bMD\b).{0,160}(?:appoint|resign|cessation|step(?:s|ped)? down|"
                                   r"reinstat)", re.I | re.S)),
    ("cfo_change", "?", re.compile(r"(?:appoint|resign|cessation).{0,160}(?:chief financial|\bCFO\b)|"
                                   r"(?:chief financial|\bCFO\b).{0,160}(?:appoint|resign|cessation)", re.I | re.S)),
    ("order_win", "up", re.compile(r"^bagging|receiving of orders|order(?:s)? (?:worth|valued|received|win|inflow)|"
                                   r"\b(?:wins?|won|bags?|bagged|secures?|secured|receives?|received|receipt of)\b.{0,40}\b(?:orders?|contracts?)\b|"
                                   r"letter of (?:award|intent)|\bLoA\b|contract (?:worth|awarded)|award(?:ed)? "
                                   r"(?:of )?(?:a )?(?:contract|order|project)", re.I)),
    ("demerger", "?", re.compile(r"demerger|scheme of arrangement", re.I)),
    ("acquisition", "?", re.compile(r"^acquisition|amalgamation|merger|acquir(?:e|es|ed|ing) (?:a |the )?(?:stake|"
                                    r"company|business|\d)", re.I)),
    ("fundraise", "?", re.compile(r"qualified institutions? placement|\bQIP\b|preferential (?:issue|allotment)|"
                                  r"rights issue|fund ?rais", re.I)),
    ("pledge_release", "up", re.compile(r"(?:release|revocation|revoke) of (?:the )?pledge|pledge (?:release|revok)", re.I)),
    ("pledge_created", "down", re.compile(r"(?:creation|invocation) of (?:the )?(?:pledge|encumbrance)|pledge (?:creat|invok)",
                                          re.I)),
    ("insider_trade", "?", re.compile(r"insider trading|\bPIT\b|regulation 7\s*\(\s*2\s*\)", re.I)),
    ("sast", "?", re.compile(r"takeover regulations|substantial acquisition|regulation 29|\bSAST\b", re.I)),
    ("results", "?", re.compile(r"financial results?|integrated filing.{0,5}financial|results for the (?:quarter|period)",
                                re.I)),
    ("dividend", "up", re.compile(r"dividend", re.I)),
    ("capacity", "up", re.compile(r"capacity expansion|commercial production|commissioning|commence(?:d|ment of) "
                                  r"(?:commercial )?(?:operations|production)", re.I)),
]
EVENT_TYPES = [t for t, _, _ in EVENT_RULES]
EVENT_DIRECTION = {t: d for t, d, _ in EVENT_RULES}
EVENT_FAMILY = {  # a rare type falls back to its family when it has too few past events
    "ceo_change": "management", "cfo_change": "management", "auditor_resignation": "management",
    "board_meeting_buyback": "board_meeting", "board_meeting_bonus_split": "board_meeting",
    "board_meeting_fundraise": "board_meeting", "board_meeting_results": "board_meeting",
    "buyback": "corporate_action", "bonus_split": "corporate_action", "dividend": "corporate_action",
    "order_win": "business", "acquisition": "business", "demerger": "business", "capacity": "business",
    "fundraise": "business", "rating_up": "rating", "rating_down": "rating", "default": "distress",
    "pledge_release": "pledge", "pledge_created": "pledge", "insider_trade": "smart_money", "sast": "smart_money",
    "price_query": "speculation", "media_clarification": "speculation", "results": "results",
}


def event_type(text, desc="", company=""):
    """(type, direction) for one filing, or (None, None) when it is routine."""
    if company and len(company) > 3:
        text = re.sub(re.escape(company), " ", text or "", flags=re.I)
    hay = "%s %s" % (desc or "", text or "")
    if desc in NOISE_DESC or NOISE.search(hay):
        return None, None
    for t, d, rx in EVENT_RULES:
        if rx.search(hay):
            return t, d
    return None, None


MEETING_DATE = re.compile(r"(?:to be )?held on\s+(?:\w+,?\s+)?(\d{1,2})(?:st|nd|rd|th)?[-\s]([A-Za-z]{3})[A-Za-z]*[-,\s]+(\d{4})", re.I)


def meeting_date(text):
    """'Board Meeting to be held on 08-Oct-2026 to consider...' -> '2026-10-08' or None."""
    import datetime as _dt
    m = MEETING_DATE.search(text or "")
    if not m:
        return None
    try:
        return _dt.datetime.strptime("%s %s %s" % (m.group(1), m.group(2).title(), m.group(3)), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


# Event types that come from exchange data rather than a filing's text (events.py builds them from the
# price-band files and NSE's bulk / block deal history).
DATA_TYPES = {"band_cut": ("?", "surveillance"), "band_widen": ("?", "surveillance"),
              "block_deal": ("?", "smart_money"), "bulk_net_buy": ("up", "smart_money"),
              "bulk_net_sell": ("down", "smart_money")}
for _t, (_d, _f) in DATA_TYPES.items():
    EVENT_DIRECTION[_t] = _d
    EVENT_FAMILY[_t] = _f
ALL_TYPES = EVENT_TYPES + list(DATA_TYPES)


def family_of(etype):
    return EVENT_FAMILY.get(etype, etype)
