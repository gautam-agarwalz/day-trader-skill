"""Shared plumbing for the Indian smart-money tracker (trader-pelosi) and the filings & catalyst
scanner (trader-news): one polite HTTP client for NSE / BSE / SEBI / AMFI, a dedupe state file,
and local JSON / CSV output.

The same copy lives in trader-pelosi/ and trader-news/ (a repo test keeps them identical), so
each skill folder still works on its own once copied. No language model is called anywhere:
everything here is HTTP, RSS / XML / CSV parsing and plain rules.

Hosts (all official):
  www.nseindia.in            NSE's JSON APIs (www.nseindia.com redirects here)
  nsearchives.nseindia.com   NSE RSS feeds, bhavcopy / delivery and price-band CSVs
  nsearchives.nseindia.in    filing PDFs and XBRL
  api.bseindia.com           BSE announcements API (often refuses cloud IPs: the RSS is the fallback)
  www.bseindia.com           BSE RSS feeds
  www.sebi.gov.in            SEBI's RSS feed
  www.amfiindia.com          AMFI (the monthly portfolio-disclosure directory)
"""

import csv
import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import time

NSE = "https://www.nseindia.in"
NSE_ARCHIVE = "https://nsearchives.nseindia.com"
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}
JSON_ACCEPT = "application/json, text/plain, */*"
IST = _dt.timedelta(hours=5, minutes=30)
KEEP_DAYS = 90          # how long a seen item is remembered
PAUSE = 0.4             # seconds between requests to the same site


class FeedError(Exception):
    pass


_PINNED = []


def pin_clock(text):
    """--now "2026-11-23 21:30" (India time): every date window and the state's 90 days use it."""
    _PINNED[:] = [_dt.datetime.strptime(text.strip()[:16], "%Y-%m-%d %H:%M")] if text else []


def now_ist():
    if _PINNED:
        return _PINNED[0]
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0) + IST


def safe_name(text):
    """A URL or a label -> a file name for the offline / record folders."""
    t = re.sub(r"^https?://", "", str(text))
    t = re.sub(r"[^A-Za-z0-9._-]+", "_", t).strip("_")
    return t[:150] or "x"


class Client(object):
    """requests.Session with browser headers, NSE cookie warm-up, one retry, polite pauses.

    offline=DIR  read every answer from DIR/<name> instead of the network (the tests use it)
    record=DIR   also save every answer to DIR/<name> (how the test fixtures were made)
    """

    def __init__(self, offline=None, record=None, timeout=30, pause=PAUSE):
        self.offline, self.record, self.timeout, self.pause = offline, record, timeout, pause
        self._session = None
        self._warm = set()
        self._last = {}
        self.calls = []

    # ------------------------------------------------------------ session
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update(BROWSER)
        return self._session

    def warm_up(self, site):
        """Visit the site's home page once so it sets its session cookies (NSE and BSE check them).
        A refused warm-up is not fatal: NSE's APIs often answer without it."""
        if self.offline or site in self._warm:
            return
        self._warm.add(site)
        home = {"nse": NSE + "/", "bse": "https://www.bseindia.com/"}.get(site)
        if home:
            try:
                self.session().get(home, headers={"Accept": "text/html,*/*"}, timeout=self.timeout)
            except Exception:
                pass

    def _polite(self, url):
        host = url.split("/")[2] if "://" in url else url
        wait = self.pause - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    # ------------------------------------------------------------ fetching
    def get_bytes(self, url, name=None, headers=None, params=None, site=None, max_bytes=None):
        name = name or safe_name(url)
        self.calls.append(url)
        if self.offline:
            for cand in (name, name + ".gz"):
                path = os.path.join(self.offline, cand)
                if os.path.exists(path):
                    with (gzip.open(path, "rb") if cand.endswith(".gz") else open(path, "rb")) as fh:
                        return fh.read()
            raise FeedError("no offline file %s" % os.path.join(self.offline, name))
        if site:
            self.warm_up(site)
        hdrs = dict(headers or {})
        last = None
        for attempt in (0, 1):
            self._polite(url)
            try:
                r = self.session().get(url, headers=hdrs, params=params, timeout=self.timeout, stream=bool(max_bytes))
            except Exception as e:  # DNS, proxy, TLS, timeout
                last = "could not reach %s: %s" % (url.split("/")[2], str(e)[:160])
                time.sleep(1.5)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                last = "HTTP %d" % r.status_code
                time.sleep(2.0)
                continue
            if r.status_code != 200:
                raise FeedError("%s answered HTTP %d%s" % (url.split("?")[0], r.status_code,
                                                           " (access denied: the site refuses this network)"
                                                           if r.status_code == 403 else ""))
            if max_bytes:
                body = b""
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        r.close()
                        raise FeedError("%s is over %d MB" % (url, max_bytes // 1048576))
            else:
                body = r.content
            if self.record:
                os.makedirs(self.record, exist_ok=True)
                with open(os.path.join(self.record, name), "wb") as fh:
                    fh.write(body)
            return body
        raise FeedError(last or "no answer from %s" % url)

    def get_text(self, url, name=None, **kw):
        b = self.get_bytes(url, name, **kw)
        return b.decode("utf-8-sig", "replace")

    def get_json(self, url, name=None, referer=None, site=None, params=None):
        hdrs = {"Accept": JSON_ACCEPT}
        if referer:
            hdrs["Referer"] = referer
        text = self.get_text(url, name, headers=hdrs, site=site, params=params)
        try:
            return json.loads(text)
        except ValueError:
            snippet = re.sub(r"\s+", " ", text[:120])
            raise FeedError("%s did not answer JSON (%s...)" % (url.split("?")[0], snippet))

    def nse_api(self, path, name=None):
        """GET https://www.nseindia.in<path> the way NSE's own pages do."""
        return self.get_json(NSE + path, name or safe_name("nse" + path), referer=NSE + "/", site="nse")


# ---------------------------------------------------------------- RSS (feedparser)

def parse_rss(body):
    """RSS / Atom bytes -> [{title, summary, link, published, time}] with feedparser."""
    import feedparser
    f = feedparser.parse(body)
    out = []
    for e in f.entries:
        out.append({"title": (e.get("title") or "").strip(), "summary": (e.get("summary") or "").strip(),
                    "link": (e.get("link") or "").strip(), "published": (e.get("published") or "").strip(),
                    "time": parse_when(e.get("published") or e.get("updated") or "")})
    return out


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                      "NOV", "DEC"], 1)}


def parse_when(text):
    """The date formats NSE / BSE / SEBI print -> "YYYY-MM-DD HH:MM" India time, or ""."""
    t = str(text or "").strip()
    if not t:
        return ""
    m = re.match(r"(\d{1,2})[-/ ]([A-Za-z]{3})[A-Za-z]*[-/ ,]+(\d{4})(?:[ ,T]+(\d{1,2})[:.-](\d{2}))?", t)
    if m and m.group(2).upper() in MONTHS:
        d = _dt.datetime(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)),
                         int(m.group(4) or 0), int(m.group(5) or 0))
        return d.strftime("%Y-%m-%d %H:%M")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", t)
    if m:
        return "%s-%s-%s %s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4) or "00", m.group(5) or "00")
    m = re.match(r"(?:[A-Za-z]{3},\s*)?(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2})(?::\d{2})?\s*([+-]\d{4})?", t)
    if m and m.group(2).upper() in MONTHS:
        d = _dt.datetime(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)), int(m.group(4)), int(m.group(5)))
        if m.group(6) and m.group(6) != "+0530":   # convert other offsets to IST
            off = int(m.group(6)[:3]) * 60 + int(m.group(6)[0] + m.group(6)[3:])
            d = d - _dt.timedelta(minutes=off) + IST
        return d.strftime("%Y-%m-%d %H:%M")
    m = re.match(r"(\d{4})(\d{2})(\d{2})$", t)
    if m:
        return "%s-%s-%s 00:00" % m.groups()
    return ""


# ---------------------------------------------------------------- numbers

def num(x):
    """'1,23,456.70' / ' 45.04' / '-' -> float or None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().replace(",", "")
    if t in ("", "-", "NA", "N.A.", "nil", "Nil", "null", "None"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- dedupe state

class State(object):
    """A JSON file: every item id ever flagged (with when), plus snapshots for diffs
    (pledges, surveillance lists, shareholding, fund portfolios). Repeat items are never
    flagged twice; ids are forgotten after KEEP_DAYS."""

    def __init__(self, path, keep_days=KEEP_DAYS):
        self.path, self.keep_days = path, keep_days
        self.doc = {"seen": {}, "snapshots": {}}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                if isinstance(doc, dict):
                    self.doc["seen"] = dict(doc.get("seen") or {})
                    self.doc["snapshots"] = dict(doc.get("snapshots") or {})
            except (ValueError, OSError):
                pass  # a broken state file starts fresh rather than stopping the scan

    @staticmethod
    def key(item_id):
        return hashlib.sha1(str(item_id).encode("utf-8")).hexdigest()[:20]

    def is_new(self, item_id):
        return self.key(item_id) not in self.doc["seen"]

    def mark(self, item_id, when=None):
        self.doc["seen"].setdefault(self.key(item_id), (when or now_ist()).strftime("%Y-%m-%d"))

    def snapshot(self, name):
        return self.doc["snapshots"].get(name)

    def set_snapshot(self, name, value):
        self.doc["snapshots"][name] = value

    def save(self):
        cutoff = (now_ist() - _dt.timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        self.doc["seen"] = {k: v for k, v in self.doc["seen"].items() if v >= cutoff}
        write_json(self.path, self.doc)


def fresh(items, state):
    """Mark each item new / seen against the state (and among themselves)."""
    seen_now = set()
    for it in items:
        k = State.key(it["id"])
        it["new"] = state.is_new(it["id"]) and k not in seen_now
        seen_now.add(k)
    return items


# ---------------------------------------------------------------- output

def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def append_csv(path, rows, fields):
    """Append rows (dicts) to a CSV, writing the header the first time. Lists are joined with ' | '."""
    if not rows:
        return
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: (" | ".join(map(str, v)) if isinstance(v, (list, tuple)) else
                            json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
                        for k, v in r.items()})


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def my_symbols(watchlist="trader/watchlist.json", plan="trader/plan.json", portfolio="trader/portfolio.json"):
    """The NSE symbols you care about: the scanner's hits, the planned trade and your holdings."""
    out = set()
    for h in (load_json(watchlist) or {}).get("hits", []) or []:
        if h.get("symbol"):
            out.add(_bare(h["symbol"]))
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.add(_bare(pl["symbol"]))
    for p in (load_json(portfolio) or {}).get("positions", []) or []:
        s = p.get("ticker") or p.get("symbol")
        if s:
            out.add(_bare(s))
    return out


def _bare(sym):
    s = str(sym).strip().upper()
    return s[:-3] if s.endswith((".NS", ".BO")) else s
