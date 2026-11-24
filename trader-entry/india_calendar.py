"""NSE trading holidays, one list for every tool.

Three layers, merged (later wins):
  BUILTIN                        the 2026 list from NSE's circular (weekday holidays, equity segment)
  trader/cache/nse_holidays.json NSE's own holiday API (www.nseindia.in/api/holiday-master?type=trading,
                                 segment CM), refreshed by ensure() when a year is not known yet
  trader/nse_holidays.json       your additions, {"2027-01-26": "Republic Day"}, e.g. from a new circular

    NSE_HOLIDAYS        {date: name}, one dict object shared by every importer (refreshes update it in place)
    known(year)         is that year's list loaded?  years() -> the years that are
    ensure(year)        try NSE's API once a day when the year is unknown; never raises, no network in tests
A year that is not known must not be treated as "every weekday is open": trader-risk blocks entries
on an unknown year, the other tools warn.

The same copy lives in every tool folder that needs it (a repo test keeps them identical).
"""

import datetime as _dt
import json
import os
import re

BUILTIN = {  # NSE equity segment trading holidays on weekdays, 2026 (NSE circulars)
    "2026-01-15": "Municipal Corporation Elections in Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CACHE = os.path.join("trader", "cache", "nse_holidays.json")
USER = os.path.join("trader", "nse_holidays.json")
API = "https://www.nseindia.in/api/holiday-master?type=trading"
HOME = "https://www.nseindia.in/"
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"), 1)}

NSE_HOLIDAYS = {}
_YEARS = set()


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def parse_master(doc, segment="CM"):
    """NSE's holiday-master answer -> {YYYY-MM-DD: name} for weekday holidays of the segment."""
    out = {}
    for row in (doc or {}).get(segment) or []:
        m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", str(row.get("tradingDate") or ""))
        if not m or m.group(2).upper() not in MONTHS:
            continue
        d = _dt.date(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)))
        if d.weekday() < 5:
            out[d.isoformat()] = str(row.get("description") or "holiday").strip()
    return out


def reload(cache=CACHE, user=USER):
    """Rebuild NSE_HOLIDAYS (in place) from the three layers. A year counts as known when any layer
    lists it (the built-in year, or a year NSE's API or your file covers)."""
    merged = dict(BUILTIN)
    years = {int(d[:4]) for d in BUILTIN}
    c = _read(cache) or {}
    got = c.get("holidays") or {}
    merged.update(got)
    years |= {int(y) for y in c.get("years") or []} | {int(d[:4]) for d in got}
    u = _read(user) or {}
    merged.update({k: v for k, v in u.items() if re.match(r"^\d{4}-\d{2}-\d{2}$", k)})
    years |= {int(k[:4]) for k in u if re.match(r"^\d{4}-\d{2}-\d{2}$", k)}
    NSE_HOLIDAYS.clear()
    NSE_HOLIDAYS.update(merged)
    _YEARS.clear()
    _YEARS.update(years)
    return NSE_HOLIDAYS


def known(year):
    return int(year) in _YEARS


def years():
    return tuple(sorted(_YEARS))


def ensure(year, cache=CACHE, timeout=20):
    """When `year` is not known, ask NSE's holiday API (at most once a day). True when it is known after."""
    if known(year):
        return True
    today = _dt.date.today().isoformat()
    c = _read(cache) or {}
    if c.get("tried") == today:
        return False
    try:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
                          "Referer": HOME})
        try:
            s.get(HOME, timeout=timeout)
        except Exception:
            pass
        r = s.get(API, timeout=timeout)
        got = parse_master(r.json()) if r.status_code == 200 else {}
    except Exception:
        got = {}
    doc = {"tried": today, "holidays": dict(c.get("holidays") or {}, **got),
           "years": sorted({int(d[:4]) for d in got} | set(c.get("years") or []))}
    try:
        d = os.path.dirname(cache)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
    except OSError:
        pass
    reload(cache)
    return known(year)


reload()
