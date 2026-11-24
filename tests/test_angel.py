"""Step 5 of the audit roadmap: Angel One SmartAPI as a data source (never orders). Offline, with answers in
the shapes SmartAPI documents (loginByPassword, getCandleData, market quote FULL, the scrip master)."""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

from _load import load, ROOT

sys.path.insert(0, os.path.join(ROOT, "trader-scanner"))
import angel_one  # noqa: E402  (the plain module the scanner's tools import)
nse_data = load("trader-scanner", "nse_data")
ib = load("trader-scanner", "india_bars")

ENV = {"ANGEL_API_KEY": "key", "ANGEL_CLIENT_CODE": "A123", "ANGEL_PIN": "1234",
       "ANGEL_TOTP_SECRET": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}


def write_fixtures(d):
    os.makedirs(d, exist_ok=True)
    scrips = [{"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "", "instrumenttype": "",
               "exch_seg": "NSE"},
              {"token": "13611", "symbol": "KEC-EQ", "name": "KEC", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
              {"token": "55001", "symbol": "RELIANCE26DECFUT", "name": "RELIANCE", "expiry": "29DEC2026",
               "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
              {"token": "55000", "symbol": "RELIANCE26NOVFUT", "name": "RELIANCE", "expiry": "26NOV2026",
               "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
              {"token": "9", "symbol": "NIFTY", "name": "NIFTY", "expiry": "", "instrumenttype": "AMXIDX",
               "exch_seg": "NSE"}]
    candles = {"status": True, "message": "SUCCESS", "errorcode": "",
               "data": [["2026-10-23T09:20:00+05:30", 301.0, 302.0, 300.5, 301.5, 12000],
                        ["2026-10-23T09:15:00+05:30", 300.0, 301.2, 299.0, 301.0, 25000]]}
    daily = {"status": True, "message": "SUCCESS", "errorcode": "",
             "data": [["2026-10-%02dT00:00:00+05:30" % d_, 300 + d_, 302 + d_, 299 + d_, 301 + d_, 1e6]
                      for d_ in (20, 21, 22, 23)]}
    quote = {"status": True, "message": "SUCCESS", "errorcode": "",
             "data": {"fetched": [{"exchange": "NSE", "tradingSymbol": "KEC-EQ", "symbolToken": "13611", "ltp": 295.25,
                                   "open": 301.0, "high": 302.8, "low": 294.1, "close": 300.15, "tradeVolume": 1448257,
                                   "opnInterest": 0, "exchFeedTime": "23-Nov-2026 15:29:59"}], "unfetched": []}}
    files = {"scrips": scrips, "candles_KEC_5m": candles, "candles_KEC_1d": daily, "quote_NSE_0": quote,
             "login": {"status": True, "message": "SUCCESS", "data": {"jwtToken": "jwt-1", "refreshToken": "r",
                                                                       "feedToken": "f"}}}
    for k, v in files.items():
        with open(os.path.join(d, "angel_%s.json" % k), "w") as fh:
            json.dump(v, fh)
    return d


class Basics(unittest.TestCase):
    def test_totp_rfc_6238(self):
        # RFC 6238's SHA1 test vector: secret "12345678901234567890", T = 59 s -> 94287082 (8 digits)
        self.assertEqual(angel_one.totp(ENV["ANGEL_TOTP_SECRET"], 59, digits=8), "94287082")
        self.assertEqual(angel_one.totp(ENV["ANGEL_TOTP_SECRET"], 1111111109, digits=8), "07081804")
        self.assertEqual(len(angel_one.totp(ENV["ANGEL_TOTP_SECRET"])), 6)

    def test_configuration(self):
        self.assertTrue(angel_one.configured(ENV))
        self.assertFalse(angel_one.configured(dict(ENV, ANGEL_PIN="")))
        self.assertEqual(angel_one.provider({"TRADER_INTRADAY": "Yahoo"}), "yahoo")
        self.assertEqual(angel_one.provider({}), "auto")

    def test_tokens(self):
        t = angel_one.build_tokens(json.load(open(os.path.join(write_fixtures(tempfile.mkdtemp()), "angel_scrips.json"))))
        self.assertEqual((t["NSE:RELIANCE"], t["NSE:KEC"], t["NFO:RELIANCE"]), ("2885", "13611", "55000"))
        self.assertNotIn("NSE:NIFTY", t, "indices are not equities")


class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = write_fixtures(os.path.join(self.tmp, "fix"))
        self.api = angel_one.Angel(env=ENV, offline=self.fix, cache_dir=os.path.join(self.tmp, "cache"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_candles_become_a_chart_the_tools_read(self):
        rows = self.api.candles("KEC", "5m")
        self.assertEqual(rows[0][0], "2026-10-23T09:15:00+05:30", "sorted oldest first")
        text = self.api.chart_json("KEC", "5m")
        import datetime
        bars = ib.parse(text, datetime.datetime(2026, 10, 23, 5, 0))
        self.assertEqual([(b["date"], b["time"], b["c"], b["v"]) for b in bars],
                         [("2026-10-23", "09:15", 301.0, 25000.0), ("2026-10-23", "09:20", 301.5, 12000.0)])
        self.assertEqual(json.loads(text)["chart"]["result"][0]["meta"]["source"], "angelone")

    def test_quotes(self):
        q = self.api.quotes(["KEC", "NOPE"])
        self.assertEqual(set(q), {"KEC"})
        self.assertEqual((q["KEC"]["price"], q["KEC"]["prev_close"], q["KEC"]["source"]), (295.25, 300.15, "angelone"))

    def test_an_error_answer_raises(self):
        with open(os.path.join(self.fix, "angel_candles_RELIANCE_5m.json"), "w") as fh:
            json.dump({"status": False, "message": "Invalid Token", "errorcode": "AG8001"}, fh)
        with self.assertRaises(angel_one.AngelError) as cm:
            self.api.candles("RELIANCE", "5m")
        self.assertIn("AG8001", str(cm.exception))


class Session(unittest.TestCase):
    def test_login_is_cached_for_the_day_and_private(self):
        tmp = tempfile.mkdtemp()
        try:
            api = angel_one.Angel(env=ENV, cache_dir=tmp)
            sent = []

            def fake_post(path, body, name, auth=True):
                sent.append((path, body, auth))
                return {"jwtToken": "jwt-1"}
            api._post = fake_post
            self.assertEqual(api._login(), "jwt-1")
            self.assertEqual(sent[0][0], angel_one.LOGIN)
            self.assertEqual(sent[0][1]["clientcode"], "A123")
            self.assertEqual(len(sent[0][1]["totp"]), 6)
            path = os.path.join(tmp, "angel_session.json")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            again = angel_one.Angel(env=ENV, cache_dir=tmp)
            again._post = lambda *a, **k: self.fail("a second login the same day")
            self.assertEqual(again._login(), "jwt-1")
        finally:
            shutil.rmtree(tmp)

    def test_no_keys_no_login(self):
        with self.assertRaises(angel_one.AngelError):
            angel_one.Angel(env={})._login()


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix = write_fixtures(os.path.join(self.tmp, "fix"))
        self.saved_env = {k: os.environ.get(k) for k in list(ENV) + ["TRADER_INTRADAY"]}
        os.environ.update(ENV)
        os.environ.pop("TRADER_INTRADAY", None)
        self.real = angel_one.Angel
        fix, tmp = self.fix, self.tmp
        angel_one.Angel = lambda *a, **k: self.real(env=ENV, offline=fix, cache_dir=os.path.join(tmp, "c"))

    def tearDown(self):
        angel_one.Angel = self.real
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def test_intraday_bars_come_from_angel_first(self):
        text = ib.fetch_text("KEC", "5m")
        self.assertEqual(json.loads(text)["chart"]["result"][0]["meta"]["source"], "angelone")
        self.assertEqual(ib.LAST_SOURCE["KEC"], "angelone")

    def test_angel_only_mode_says_why_it_failed(self):
        os.environ["TRADER_INTRADAY"] = "angel"
        with self.assertRaises(ib.BarsError) as cm:
            ib.fetch_text("RELIANCE", "5m")           # no RELIANCE candles saved
        self.assertIn("Angel One", str(cm.exception))

    def test_nse_backup_hook_uses_angel_before_the_cache(self):
        q, err = nse_data.fetch_backup_data(["KEC"], "quotes", cache_dir=os.path.join(self.tmp, "none"),
                                            reasons={"KEC": "timed out"})
        self.assertEqual((q["KEC"]["price"], q["KEC"]["source"], q["KEC"]["time"]),
                         (295.25, "angelone", "2026-11-23T09:59:59Z"))
        d, err = nse_data.fetch_backup_data(["KEC"], "daily", cache_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(d["KEC"]["meta"]["source"], "angelone")
        self.assertEqual(len(d["KEC"]["c"]), 4)


if __name__ == "__main__":
    unittest.main()
