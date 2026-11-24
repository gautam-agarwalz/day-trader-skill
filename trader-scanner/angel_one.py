"""Angel One SmartAPI as a DATA source: 5-minute / hourly / daily candles and live quotes with open interest.
No order functions exist in this module, by design (a repo test checks it); orders stay manual, through
/trader-broker and your own yes.

Credentials come from environment variables only, never from a file in this repo:
    ANGEL_API_KEY       the SmartAPI app's API key (smartapi.angelone.in -> My Apps)
    ANGEL_CLIENT_CODE   your Angel One client code
    ANGEL_PIN           your 4-digit login PIN
    ANGEL_TOTP_SECRET   the TOTP secret shown when you enabled TOTP (the base32 text, not a 6-digit code)
With all four set, india_bars.fetch_text uses Angel One for NSE intraday candles (Yahoo stays the fallback),
and nse_data.fetch_backup_data uses it when NSE's MCP servers fail. TRADER_INTRADAY=yahoo turns it off,
TRADER_INTRADAY=angel makes it the only source.

    Angel().candles("RELIANCE", "5m", days=90)    [[iso time, o, h, l, c, v], ...]
    Angel().chart_json("RELIANCE", "5m")          the same candles as Yahoo chart JSON (what the tools parse)
    Angel().quotes(["RELIANCE", "TCS"])           {SYM: {price, open, high, low, prev_close, volume, oi, time}}

The session token (valid for the day) is cached in trader/cache/angel_session.json, readable only by you.
Symbol tokens come from Angel One's public scrip master (cached a day). SmartAPI's limits: about 3 candle
requests a second; this module pauses between calls. Max days per candle request: 5m 100, 15m 200, 1h 400,
1d 2000.
"""

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import struct
import time

HOSTS = ("https://apiconnect.angelone.in", "https://apiconnect.angelbroking.com")
LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
QUOTE = "/rest/secure/angelbroking/market/v1/quote/"
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
ENV = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
INTERVALS = {"1m": ("ONE_MINUTE", 30), "3m": ("THREE_MINUTE", 60), "5m": ("FIVE_MINUTE", 100),
             "15m": ("FIFTEEN_MINUTE", 200), "30m": ("THIRTY_MINUTE", 200), "60m": ("ONE_HOUR", 400),
             "1d": ("ONE_DAY", 2000)}
DEFAULT_DAYS = {"5m": 90, "15m": 120, "60m": 365, "1d": 1825}
CACHE = os.path.join("trader", "cache")
PAUSE = 0.4
IST = _dt.timedelta(hours=5, minutes=30)


class AngelError(Exception):
    pass


def _now_ist():
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + IST


def configured(env=None):
    env = os.environ if env is None else env
    return all(env.get(k) for k in ENV)


def provider(env=None):
    """'auto' (Angel when configured, else Yahoo), 'angel' or 'yahoo', from TRADER_INTRADAY."""
    env = os.environ if env is None else env
    p = (env.get("TRADER_INTRADAY") or "auto").strip().lower()
    return p if p in ("auto", "angel", "yahoo") else "auto"


def totp(secret, for_time=None, step=30, digits=6):
    """RFC 6238 TOTP (HMAC-SHA1), the 6-digit code Angel One's login asks for."""
    s = secret.strip().replace(" ", "").upper()
    key = base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8))
    counter = int((time.time() if for_time is None else for_time) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _ist_iso_to_unix(text):
    """'2026-10-23T09:15:00+05:30' -> unix seconds."""
    t = str(text)
    base = _dt.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    off = IST
    if len(t) > 19 and t[19] in "+-":
        sign = 1 if t[19] == "+" else -1
        off = sign * _dt.timedelta(hours=int(t[20:22]), minutes=int(t[23:25]))
    elif t.endswith("Z"):
        off = _dt.timedelta(0)
    return int(((base - off) - _dt.datetime(1970, 1, 1)).total_seconds())


def to_chart_json(symbol, rows, interval="5m"):
    """Angel candles -> Yahoo chart JSON text, so india_bars.parse and timemachine read them unchanged."""
    ts = [_ist_iso_to_unix(r[0]) for r in rows]
    col = lambda i: [float(r[i]) for r in rows]  # noqa: E731
    doc = {"chart": {"result": [{"meta": {"symbol": "%s.NS" % symbol.upper(), "dataGranularity": interval,
                                          "currency": "INR", "source": "angelone",
                                          "regularMarketPrice": float(rows[-1][4]) if rows else None},
                                 "timestamp": ts,
                                 "indicators": {"quote": [{"open": col(1), "high": col(2), "low": col(3),
                                                           "close": col(4), "volume": col(5)}]}}],
                     "error": None}}
    return json.dumps(doc)


class Angel(object):
    """A data-only SmartAPI client. offline=DIR reads saved answers (angel_<name>.json) instead of the network."""

    def __init__(self, env=None, offline=None, record=None, cache_dir=CACHE, timeout=20, pause=PAUSE):
        self.env = os.environ if env is None else env
        self.offline, self.record, self.cache_dir = offline, record, cache_dir
        self.timeout, self.pause = timeout, pause
        self._jwt, self._tokens, self._last = None, None, 0.0

    # ---------------------------------------------------------- transport
    def _headers(self, auth=True):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER",
             "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.env.get("ANGEL_API_KEY", "")}
        if auth:
            h["Authorization"] = "Bearer %s" % self._login()
        return h

    def _read(self, name):
        path = os.path.join(self.offline, "angel_%s.json" % name)
        if not os.path.exists(path):
            raise AngelError("no offline answer %s" % path)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _post(self, path, body, name, auth=True):
        if self.offline:
            doc = self._read(name)
        else:
            import requests
            wait = self.pause - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            last = None
            doc = None
            for host in HOSTS:
                try:
                    r = requests.post(host + path, json=body, headers=self._headers(auth), timeout=self.timeout)
                    self._last = time.time()
                    if r.status_code == 429:
                        time.sleep(2.0)
                        r = requests.post(host + path, json=body, headers=self._headers(auth), timeout=self.timeout)
                    doc = r.json()
                    break
                except Exception as e:  # DNS, TLS, proxy: try the other host once
                    last = str(e)[:160]
            if doc is None:
                raise AngelError("could not reach Angel One SmartAPI: %s" % last)
            if self.record:
                os.makedirs(self.record, exist_ok=True)
                with open(os.path.join(self.record, "angel_%s.json" % name), "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
        if not isinstance(doc, dict) or not doc.get("status"):
            raise AngelError("Angel One said %s (%s)" % ((doc or {}).get("message") or "no answer",
                                                         (doc or {}).get("errorcode") or "-"))
        return doc.get("data")

    # ---------------------------------------------------------- session
    def _session_path(self):
        return os.path.join(self.cache_dir, "angel_session.json")

    def _login(self):
        if self._jwt:
            return self._jwt
        if self.offline:
            self._jwt = "offline"
            return self._jwt
        if not configured(self.env):
            raise AngelError("Angel One is not configured: set %s" % ", ".join(ENV))
        today = _now_ist().strftime("%Y-%m-%d")
        try:
            with open(self._session_path(), encoding="utf-8") as fh:
                s = json.load(fh)
            if s.get("day") == today and s.get("client") == self.env["ANGEL_CLIENT_CODE"] and s.get("jwt"):
                self._jwt = s["jwt"]
                return self._jwt
        except (OSError, ValueError):
            pass
        body = {"clientcode": self.env["ANGEL_CLIENT_CODE"], "password": self.env["ANGEL_PIN"],
                "totp": totp(self.env["ANGEL_TOTP_SECRET"])}
        data = self._post(LOGIN, body, "login", auth=False)
        self._jwt = (data or {}).get("jwtToken")
        if not self._jwt:
            raise AngelError("Angel One login gave no token")
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            fd = os.open(self._session_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"day": today, "client": self.env["ANGEL_CLIENT_CODE"], "jwt": self._jwt}, fh)
        except OSError:
            pass
        return self._jwt

    # ---------------------------------------------------------- symbols
    def tokens(self):
        """{"NSE:RELIANCE": token, "NFO:RELIANCE": nearest-month FUTSTK token} from the scrip master (cached daily)."""
        if self._tokens is not None:
            return self._tokens
        path = os.path.join(self.cache_dir, "angel_tokens.json")
        today = _now_ist().strftime("%Y-%m-%d")
        if not self.offline:
            try:
                with open(path, encoding="utf-8") as fh:
                    c = json.load(fh)
                if c.get("day") == today:
                    self._tokens = c["tokens"]
                    return self._tokens
            except (OSError, ValueError, KeyError):
                pass
        if self.offline:
            rows = self._read("scrips")
        else:
            import requests
            try:
                rows = requests.get(SCRIP_MASTER, timeout=120).json()
            except Exception as e:
                raise AngelError("Angel One's scrip master did not load: %s" % str(e)[:120])
        self._tokens = build_tokens(rows)
        if not self.offline:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"day": today, "tokens": self._tokens}, fh)
            except OSError:
                pass
        return self._tokens

    def token(self, symbol, exch="NSE"):
        t = self.tokens().get("%s:%s" % (exch, symbol.upper()))
        if not t:
            raise AngelError("no Angel One token for %s on %s" % (symbol, exch))
        return t

    # ---------------------------------------------------------- data
    def candles(self, symbol, interval="5m", days=None, end=None):
        """[[iso time, o, h, l, c, v], ...] oldest first, up to the interval's per-request limit."""
        if interval not in INTERVALS:
            raise AngelError("interval %s is not one of %s" % (interval, ", ".join(INTERVALS)))
        name, cap = INTERVALS[interval]
        days = min(cap, days or DEFAULT_DAYS.get(interval, cap))
        end = end or _now_ist()
        start = end - _dt.timedelta(days=days)
        body = {"exchange": "NSE", "symboltoken": self.token(symbol), "interval": name,
                "fromdate": start.strftime("%Y-%m-%d 09:15"), "todate": end.strftime("%Y-%m-%d %H:%M")}
        data = self._post(CANDLES, body, "candles_%s_%s" % (symbol.upper(), interval))
        rows = [r for r in (data or []) if isinstance(r, list) and len(r) >= 6 and None not in r[1:5]]
        if not rows:
            raise AngelError("Angel One has no %s candles for %s" % (interval, symbol))
        return sorted(rows, key=lambda r: r[0])

    def chart_json(self, symbol, interval="5m", days=None):
        return to_chart_json(symbol, self.candles(symbol, interval, days), interval)

    def quotes(self, symbols, fno=False):
        """{SYM: {price, open, high, low, prev_close, volume, oi, time, source}} (FULL mode, 50 a request).
        fno=True quotes the nearest stock future (its open interest) instead of the share."""
        exch = "NFO" if fno else "NSE"
        by_token = {}
        for s in symbols:
            try:
                by_token[self.token(s, exch)] = s.upper()
            except AngelError:
                continue
        out = {}
        toks = list(by_token)
        for i in range(0, len(toks), 50):
            data = self._post(QUOTE, {"mode": "FULL", "exchangeTokens": {exch: toks[i:i + 50]}},
                              "quote_%s_%d" % (exch, i // 50))
            for q in (data or {}).get("fetched") or []:
                sym = by_token.get(str(q.get("symbolToken")))
                if not sym:
                    continue
                out[sym] = {"symbol": sym, "price": q.get("ltp"), "open": q.get("open"), "high": q.get("high"),
                            "low": q.get("low"), "prev_close": q.get("close"), "volume": q.get("tradeVolume"),
                            "oi": q.get("opnInterest"), "time": q.get("exchFeedTime") or "", "source": "angelone"}
        return out


def build_tokens(rows):
    """The scrip master -> {"NSE:SYM": token for SYM-EQ, "NFO:SYM": the nearest FUTSTK token}."""
    out, near = {}, {}
    for r in rows or []:
        seg, sym, tok = r.get("exch_seg"), str(r.get("symbol") or ""), str(r.get("token") or "")
        if seg == "NSE" and sym.endswith("-EQ"):
            out["NSE:%s" % sym[:-3]] = tok
        elif seg == "NFO" and r.get("instrumenttype") == "FUTSTK":
            try:
                exp = _dt.datetime.strptime(str(r.get("expiry")).title(), "%d%b%Y").date()
            except ValueError:
                continue
            name = str(r.get("name") or "").upper()
            if name and (name not in near or exp < near[name][0]):
                near[name] = (exp, tok)
    out.update({"NFO:%s" % n: t for n, (_, t) in near.items()})
    return out
