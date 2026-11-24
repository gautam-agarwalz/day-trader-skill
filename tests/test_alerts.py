import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")
nse = alerts.nse_data

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
DT = datetime.datetime

# NSE equity-segment trading holidays on weekdays, 2026 (NSE circulars, incl. the Jan 15 civic-poll closure)
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
            "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
            "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.NSE_EARLY_CLOSE, {})
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_ist_has_no_daylight_saving(self):
        for utc in (DT(2026, 1, 15, 3, 45), DT(2026, 3, 8, 7, 0), DT(2026, 7, 1, 12, 0), DT(2026, 11, 1, 6, 0)):
            self.assertEqual(alerts.to_ist(utc) - utc, datetime.timedelta(hours=5, minutes=30))

    def test_parse_now(self):
        self.assertEqual(ist("2026-10-13T09:15"), DT(2026, 10, 13, 3, 45))
        self.assertEqual(ist("2026-10-19 10:00"), DT(2026, 10, 19, 4, 30))
        self.assertEqual(ist("2026-12-01T09:15"), DT(2026, 12, 1, 3, 45))
        self.assertEqual(ist("2026-10-13T20:00:01Z"), DT(2026, 10, 13, 20, 0, 1))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-10-13T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("09:15", "15:30"))
        self.assertFalse(alerts.market_status(ist("2026-10-13T09:14"))["open"])
        self.assertTrue(alerts.market_status(ist("2026-10-13T09:15"))["open"])
        self.assertEqual(alerts.market_status(ist("2026-10-13T09:14"))["next_open"], "2026-10-13 09:15")
        self.assertTrue(alerts.market_status(ist("2026-10-13T15:29"))["open"])
        after = alerts.market_status(ist("2026-10-13T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["next_open"], "2026-10-14 09:15")
        dussehra = alerts.market_status(ist("2026-10-20T11:00"))
        self.assertFalse(dussehra["open"])
        self.assertIn("Dussehra", dussehra["reason"])
        self.assertEqual(dussehra["next_open"], "2026-10-21 09:15")
        polls = alerts.market_status(ist("2026-01-15T10:00"))
        self.assertFalse(polls["open"])
        self.assertIn("Municipal Corporation Elections", polls["reason"])
        xmas = alerts.market_status(ist("2026-12-24T15:00"))
        self.assertTrue(xmas["open"])
        self.assertIsNone(xmas["early_close"])
        xmas = alerts.market_status(ist("2026-12-25T10:00"))
        self.assertFalse(xmas["open"])
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        self.assertEqual(alerts.market_status(ist("2026-10-17T10:00"))["reason"], "weekend")
        self.assertFalse(alerts.market_status(ist("2027-03-01T10:00"))["calendar_known"])


class Quotes(unittest.TestCase):
    def test_a_real_nse_quote(self):
        # recorded from NSE's CM Market Live MCP server (cm_get_stock_quote RELIANCE), 2026-11-21
        with open(os.path.join(QDIR, "nse-RELIANCE.json")) as fh:
            q = nse.parse_live_quote(json.load(fh), "RELIANCE")
        self.assertEqual((q["price"], q["prev_close"], q["open"], q["high"], q["low"]),
                         (1377.5, 1375.6, 1369.0, 1383.0, 1356.1))
        self.assertEqual((q["volume"], q["currency"], q["source"]), (13167730, "INR", "nse"))
        self.assertEqual(q["time"], "2026-11-20T10:30:28Z")         # 16:00:28 IST

    def test_the_fixture_quotes(self):
        q, errors = alerts.get_quotes(["AAPL", "TSLA"], QDIR)
        self.assertEqual((q["AAPL"]["price"], q["AAPL"]["prev_close"]), (341.27, 340.97))
        self.assertEqual(q["AAPL"]["time"], "2026-10-13T10:15:01Z")
        self.assertEqual((q["TSLA"]["price"], q["TSLA"]["prev_close"], round(q["TSLA"]["open"], 2)),
                         (366.475, 354.72, 357.48))
        self.assertEqual(q["TSLA"]["time"], "2026-10-19T05:07:13Z")

    def test_symbols_are_nse_symbols(self):
        self.assertEqual([nse.nse_symbol(s) for s in ("reliance", "TCS.NS", " m&m.ns ")], ["RELIANCE", "TCS", "M&M"])
        q, _ = alerts.get_quotes(["reliance.ns"], QDIR)
        self.assertEqual(list(q), ["RELIANCE"])

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["AAPL", "KO", "ZZZZ", "NOPE"], QDIR)
        self.assertEqual(sorted(q), ["AAPL", "KO"])
        self.assertEqual(q["KO"]["price"], 90.11)
        self.assertEqual(len(errors), 2)
        self.assertIn("Symbol 'ZZZZ' not found", errors[0])
        self.assertIn("no offline quote file", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "nse-TSLA.json"), "rb") as src:
                with gzip.open(os.path.join(d, "nse-TSLA.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["TSLA"], d)
            self.assertEqual((q["TSLA"]["price"], errors), (366.475, []))
        finally:
            shutil.rmtree(d)


def fixture(sym):
    with open(os.path.join(QDIR, "nse-%s.json" % sym)) as fh:
        return json.load(fh)


class NseClient(unittest.TestCase):
    """nse_data without the network: call_tools is swapped for a fake NSE."""

    def setUp(self):
        self.saved = nse.call_tools
        self.cache = tempfile.mkdtemp()
        self.calls = []

    def tearDown(self):
        nse.call_tools = self.saved
        shutil.rmtree(self.cache)

    def nse_answers(self, answers):
        """answers: {SYM: (ok, payload)} for cm_get_stock_quote; anything else: a timeout."""
        def fake(url, calls, **kw):
            self.calls.append((url, calls))
            return [answers.get(args.get("symbol"), (False, "timed out after 45s")) for _, args in calls]
        nse.call_tools = fake

    def test_live_quotes_go_to_the_official_server_one_call_a_symbol(self):
        self.nse_answers({"RELIANCE": (True, fixture("RELIANCE")), "KO": (True, fixture("KO"))})
        q, errors = nse.fetch_quotes(["RELIANCE", "ko.ns"], cache_dir=self.cache)
        self.assertEqual((q["RELIANCE"]["price"], q["KO"]["price"], errors), (1377.5, 90.11, {}))
        url, calls = self.calls[0]
        self.assertEqual(url, "https://mcp.nseindia.in/cmmkt/mcp")
        self.assertEqual(calls, [("cm_get_stock_quote", {"symbol": "RELIANCE"}), ("cm_get_stock_quote", {"symbol": "KO"})])
        self.assertTrue(os.path.exists(os.path.join(self.cache, "quotes.csv")))

    def test_a_timeout_a_failure_or_an_empty_answer_goes_to_the_backup(self):
        self.nse_answers({"RELIANCE": (True, fixture("RELIANCE")), "KO": (True, fixture("KO")),
                          "AAPL": (True, fixture("AAPL"))})
        nse.fetch_quotes(["RELIANCE", "KO", "AAPL"], cache_dir=self.cache)      # fills the cache
        self.nse_answers({"KO": (False, "failed: connection reset"),
                          "AAPL": (True, {"updatedAt": "x", "stock": None})})  # RELIANCE: timed out
        q, errors = nse.fetch_quotes(["RELIANCE", "KO", "AAPL", "NEW"], cache_dir=self.cache)
        self.assertEqual(sorted(q), ["AAPL", "KO", "RELIANCE"])
        for s in ("RELIANCE", "KO", "AAPL"):
            self.assertEqual(q[s]["source"], "cache", s)
        self.assertEqual(q["RELIANCE"]["price"], 1377.5)
        self.assertEqual(q["RELIANCE"]["time"], "2026-11-20T10:30:28Z")     # the original quote time: stale stays stale
        self.assertEqual(list(errors), ["NEW"])
        self.assertIn("timed out", errors["NEW"])
        self.assertIn("backup: no cached quote", errors["NEW"])

    def test_the_backup_hook_alone(self):
        q, errors = nse.fetch_backup_data(["TCS"], "quotes", cache_dir=self.cache, reasons={"TCS": "timed out"})
        self.assertEqual((q, errors), ({}, {"TCS": "timed out; backup: no cached quote"}))
        with self.assertRaises(nse.NSEError):
            nse.fetch_backup_data(["TCS"], "ticks", cache_dir=self.cache)

    def test_nse_error_answers_are_failures(self):
        class R(object):
            def __init__(self, text, err=False):
                self.content = [type("C", (), {"text": text})()] if text is not None else []
                self.is_error = err
        self.assertEqual(nse._payload(R('{"stock": {"symbol": "TCS"}}')), {"stock": {"symbol": "TCS"}})
        for r, words in ((R('{"error": "Symbol \'X\' not found"}'), "not found"), (R(""), "empty"),
                         (R(None), "empty"), (R("{}"), "empty"), (R("boom", True), "NSE error"),
                         (R("<html>"), "not JSON")):
            with self.assertRaises(nse.NSEError) as cm:
                nse._payload(r)
            self.assertIn(words, str(cm.exception))

    def test_no_sdk_or_no_server_fails_every_call_with_the_reason(self):
        res = nse.call_tools("https://127.0.0.1:9/mcp", [("cm_get_stock_quote", {"symbol": "TCS"})] * 2,
                             timeout=5, session_timeout=10)
        self.assertEqual(len(res), 2)
        self.assertFalse(res[0][0])
        self.assertTrue(res[0][1].startswith(("could not reach NSE", "timed out", "the mcp SDK")), res[0][1])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["AAPL", "BAC", "KO", "TSLA"], QDIR)
        self.open = alerts.market_status(ist("2026-10-13T10:00"))
        self.now = ist("2026-10-13T10:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "aapl-340", "symbol": "AAPL", "type": "above", "price": 340}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("AAPL is ₹341.27: at or above your alert level ₹340", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "bac-stop", "symbol": "BAC", "type": "stop", "price": 54, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop ₹54", rings[0]["message"])
        self.q["BAC"]["price"] = 55.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("bac-stop", st["rung"])
        self.q["BAC"]["price"] = 53.5
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "bac-3", "symbol": "BAC", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "bac-1", "symbol": "BAC", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("-1.14%", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "bac-up", "symbol": "BAC", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        tsla = [{"id": "tsla-3", "symbol": "TSLA", "type": "pct_move", "pct": 3}]
        rings, _ = self.at("2026-10-19 10:37", tsla)
        self.assertIn("TSLA moved +3.31% to ₹366.48 (from ₹354.72)", rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-10-13T08:00"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-10-13T08:00"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-10-13T11:00"))[0], [])
        nxt = alerts.market_status(ist("2026-10-14T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-10-14T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by_close(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("tsla-plan-stop", "stop"), ("tsla-plan-target", "target"), ("tsla-flat-by", "time"),
                          ("tsla-catalysts", "catalyst")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (358.32, 370.04))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"]), ("15:15", "flat by close", True))
        self.assertTrue(all(r["date"] == "2026-10-19" for r in rules))
        rings, st = self.at("2026-10-19 10:37", rules)          # the 10:37 quote, 366.475
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["TSLA"]["price"] = 358.10
        rings, st = self.at("2026-10-19 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["tsla-plan-stop"])
        self.assertIn("TSLA is ₹358.10: at or below your stop ₹358.32 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["TSLA"]["price"] = 370.04
        self.assertEqual([r["id"] for r in self.at("2026-10-19 12:00", rules, st, q)[0]], ["tsla-plan-target"])

    def test_flat_by_close_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-19 15:14", rules)[0], [])
        rings, st = self.at("2026-10-19 15:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol TSLA", rings[0]["message"])
        self.assertEqual(self.at("2026-10-19 15:29", rules, st)[0], [])
        rings, _ = self.at("2026-10-19 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-21 15:16", rules, st)[0], [])     # plan rules: their day only

    def test_a_time_alert_skips_weekends_and_nse_holidays(self):
        rule = [{"id": "flat", "type": "time", "at": "15:15", "label": "flat by close", "before_close": True}]
        self.assertEqual(self.at("2026-10-17 15:16", rule)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-11-10 15:16", rule)[0], [])         # Diwali Balipratipada
        self.assertEqual(self.at("2026-12-01 15:14", rule)[0], [])
        rings, st = self.at("2026-12-01 15:15", rule)                      # no daylight saving in IST
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["flat"], "2026-12-01T09:45:00Z")

    def test_a_time_alert_moves_with_an_early_close(self):
        # the NSE has no scheduled early close; if it ever shortens a session, "before_close" follows it
        saved = dict(alerts.NSE_EARLY_CLOSE)
        alerts.NSE_EARLY_CLOSE["2026-11-27"] = "shortened session"
        try:
            rule = [{"id": "flat", "type": "time", "at": "15:15", "label": "flat by close", "before_close": True}]
            self.assertEqual(self.at("2026-11-27 12:44", rule)[0], [])
            rings, _ = self.at("2026-11-27 12:45", rule)
            self.assertIn("set for 12:45), the market closes at 13:00", rings[0]["message"])
            plain = [{"id": "t", "type": "time", "at": "15:15", "label": "check"}]
            self.assertEqual(self.at("2026-11-27 12:45", plain)[0], [])
        finally:
            alerts.NSE_EARLY_CLOSE.clear()
            alerts.NSE_EARLY_CLOSE.update(saved)

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("tsla-plan") or r["id"] in ("tsla-flat-by", "tsla-catalysts")],
                         [dict(r, date="2026-10-19") for r in plan_rules()])

    def test_a_catalyst_rule_rings_once_per_item(self):
        rule = [{"id": "tcs-catalysts", "type": "catalyst", "symbol": "TCS"}]
        item = {"id": "nse|/x.pdf", "kind": "catalyst", "tag": "orders", "category": "", "source": "NSE announcements",
                "title": "TCS: Bagging/Receiving of orders/contracts", "link": "https://nsearchives.nseindia.in/x.pdf"}
        rings, st = alerts.evaluate(rule, {}, self.open, {}, self.now, {"TCS": [item]})
        self.assertEqual(len(rings), 1)
        self.assertIn("TCS new filing (orders): TCS: Bagging/Receiving of orders/contracts", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rule, {}, self.open, st, self.now, {"TCS": [item]})[0], [])
        more = dict(item, id="pelosi|1", kind="smart_money", tag="", category="Bulk/Block deal", title="X bought TCS")
        rings, _ = alerts.evaluate(rule, {}, self.open, st, self.now, {"TCS": [item, more]})
        self.assertEqual([r["message"][:37] for r in rings], ["TCS new smart money (Bulk/Block deal)"])
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "catalyst"})

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "AAPL"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "4pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "TSLA", "stop": 358.32})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_reel_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added tsla-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-19 10:37")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-10-19 10:38")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 5 rule(s) watched today, TSLA ₹366.48.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"TSLA": 366.475})
        self.assertEqual(doc["detail"]["TSLA"]["time"], "2026-10-19T05:07:13Z")
        self.assertEqual(doc["detail"]["TSLA"]["fetched"], "2026-10-19T05:08:00Z")

        code, text, _ = self.check("2026-10-19 15:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-19 15:17")[0], 1)
        code, text, _ = self.check("2026-10-20 10:00")                # Dussehra: no session, no bell
        self.assertNotIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-21 10:00")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-21 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "tsla-catalysts", "tsla-flat-by", "tsla-plan-stop", "tsla-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("tsla-flat-by", json.load(fh)["rung"])

    def test_the_v1_portfolio_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "AAPL", "--above", "350"], ["--symbol", "BAC", "--stop", "50"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "AAPL", "--above", "350"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-10-13T20:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"AAPL": 341.27, "BAC": 53.96})

        code, text, _ = self.check("2026-10-14T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-14T09:30")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:15", "--label", "flat by close",
                             "--before-close", "--symbol", "tsla"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "TSLA", "--target", "370.04",
                             "--date", "2026-10-19"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1515", "type": "time", "at": "15:15", "label": "flat by close",
                                    "symbol": "TSLA", "before_close": True})
        self.assertEqual(rules[1], {"id": "tsla-target-370.04", "symbol": "TSLA", "type": "target",
                                    "price": 370.04, "date": "2026-10-19"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "AAPL", "KO", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-13T20:00:01Z"])
        self.assertEqual(code, 0)
        self.assertIn("AAPL   ₹341.27", text)
        self.assertIn("delayed", text)
        run(["quote", "BAC", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["AAPL", "BAC", "KO"])
        self.assertEqual(doc["detail"]["KO"]["prev_close"], 89.79)
        self.assertEqual(doc["detail"]["KO"]["fetched"], "2026-10-13T20:00:01Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "KO", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["KO", "TSLA"])

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-10-19 10:15"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Christmas", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
