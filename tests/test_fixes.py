"""Step 1 of the audit roadmap: the correctness fixes, one test each. Offline, no network."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import engine_fixtures as fx

orders = load("trader-news", "govt_orders_tracker")      # first: it puts its folder (india_http, ...) on the path
nse_data = load("trader-scanner", "nse_data")
ib = load("trader-scanner", "india_bars")
check = load("trader-risk", "check")
cal = check.india_calendar          # the copy trader-risk itself uses
costs = load("trader-timemachine", "india_costs")
im = load("trader-news", "india_market")
ih = load("trader-news", "india_http")
ticket = load("trader-broker", "ticket")
brief = load("trader-timemachine", "brief")
obs = load("trader-scanner", "order_block_scanner")
tm = load("trader-timemachine", "timemachine")


WINDOWS = {"no_entries_before": "09:30", "no_entries_after": "14:50"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class InTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)
        cal.reload()


class SplitAdjustment(unittest.TestCase):
    def rows(self):
        out = [{"date": "2022-07-%02d" % d, "open": 950.0 + d, "high": 960.0 + d, "low": 940.0 + d,
                "close": 955.0 + d, "volume": 1000} for d in range(18, 28)]
        out += [{"date": "2022-07-28", "open": 100.5, "high": 101.5, "low": 99.5, "close": 100.35, "volume": 10000},
                {"date": "2022-07-29", "open": 101.0, "high": 108.0, "low": 100.8, "close": 107.6, "volume": 9000}]
        return out

    def test_a_1_for_10_split_is_scaled_out(self):
        s = nse_data.rows_to_series(self.rows(), "TATASTEEL")
        self.assertEqual(s["meta"]["adjustments"], [{"date": "2022-07-28", "factor": 0.1}])
        self.assertAlmostEqual(s["c"][9], 98.2, places=2)           # 982 before the split, now 98.2
        self.assertEqual(s["v"][0], 10000)
        self.assertLess(abs(s["c"][10] / s["c"][9] - 1), 0.03, "no fake -90 % day left")

    def test_a_real_crash_is_not_a_split(self):
        self.assertIsNone(nse_data.split_factor(100, 78, 80))         # 22 % gap: a shock, not a ratio
        self.assertIsNone(nse_data.split_factor(100, 70, 69), "near 2/3 but too far from it")
        self.assertEqual(nse_data.split_factor(100, 50.5, 52), 0.5)   # a 1:1 bonus
        self.assertEqual(nse_data.split_factor(100, 74.8, 75.5), 0.75)  # a 1:3 bonus


class HourlyBars(unittest.TestCase):
    def test_nse_hourly_bars_start_at_9_15(self):
        ts = [int((datetime.datetime(2026, 10, 22, h, 15) - datetime.timedelta(hours=5, minutes=30)
                   - datetime.datetime(1970, 1, 1)).total_seconds()) for h in (9, 10, 11)]
        doc = {"chart": {"result": [{"meta": {"symbol": "KEC.NS", "dataGranularity": "60m"}, "timestamp": ts,
                                     "indicators": {"quote": [{"open": [1, 1, 1], "high": [2, 2, 2], "low": [0.5] * 3,
                                                               "close": [1.5] * 3, "volume": [10] * 3}]}}]}}
        bars = ib.parse(json.dumps(doc), datetime.datetime(2026, 10, 23))
        self.assertEqual([b["time"] for b in bars], ["09:15", "10:15", "11:15"])


class Costs(unittest.TestCase):
    def test_delivery_contract_note(self):
        # Rs 1,00,000 bought and sold: STT 100 + 100, stamp 15, NSE 2.97 x 2, SEBI 0.10 x 2, GST 18 % of
        # (5.94 + 0.20) = 1.11, DP 15.93, no brokerage
        ch = costs.charges("delivery", 100000, 100000)
        self.assertEqual((ch["stt"], ch["stamp"], ch["exchange"], ch["sebi"], ch["gst"], ch["dp"], ch["brokerage"]),
                         (200.0, 15.0, 5.94, 0.2, 1.11, 15.93, 0.0))
        self.assertEqual(ch["total"], 238.18)

    def test_intraday_and_futures(self):
        ch = costs.charges("intraday", 100000, 100000)
        self.assertEqual((ch["stt"], ch["stamp"], ch["brokerage"]), (25.0, 3.0, 40.0))   # Rs 20 an order cap
        ch = costs.charges("futures", 500000, 500000)
        self.assertEqual((ch["stt"], ch["stamp"]), (100.0, 10.0))

    def test_slippage_by_liquidity_and_r(self):
        self.assertEqual(costs.slippage_pct(150), 0.02)
        self.assertEqual(costs.slippage_pct(2), 0.25)
        self.assertAlmostEqual(costs.cost_r("intraday", 100.0, 99.0, turnover_cr=30), 0.1825, places=3)
        self.assertEqual(costs.after_tax("delivery", 1.0), 0.8)
        self.assertEqual(costs.after_tax("intraday", -1.0), -1.0)

    def test_the_intraday_time_machine_reports_after_costs(self):
        pool = [[(1.0, 1.002, 0.998, 1.001)] for _ in range(tm.SESSION_BARS)]
        mc = tm.simulate(pool, 100.0, 99.0, 102.0, 10, 74, replays=50, seed=1, window=0)
        self.assertNotIn("avg_R_net", mc, "simulate stays gross; time_machine adds the costs")


class Calendar(InTemp):
    def test_an_unknown_year_blocks_entries_in_risk(self):
        os.makedirs(os.path.join("trader", "cache"))
        with open(cal.CACHE, "w") as fh:
            json.dump({"tried": datetime.date.today().isoformat(), "holidays": {}, "years": []}, fh)
        cal.reload()
        reasons, _ = check.window_check(datetime.datetime(2027, 1, 4, 10, 15), WINDOWS)
        self.assertTrue(reasons and "holiday list for 2027 is not loaded" in reasons[0], reasons)

    def test_a_user_file_adds_a_year(self):
        os.makedirs("trader")
        with open(cal.USER, "w") as fh:
            json.dump({"2027-01-26": "Republic Day"}, fh)
        cal.reload()
        self.assertTrue(cal.known(2027))
        reasons, _ = check.window_check(datetime.datetime(2027, 1, 26, 10, 15), WINDOWS)
        self.assertIn("NSE holiday: Republic Day", reasons[0])
        self.assertEqual(check.window_check(datetime.datetime(2027, 1, 27, 10, 15), WINDOWS)[0], [])

    def test_nse_holiday_api_parser(self):
        doc = {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"},
                      {"tradingDate": "14-Feb-2027", "description": "a Sunday"}]}
        self.assertEqual(cal.parse_master(doc), {"2027-01-26": "Republic Day"})


class MarketCap(InTemp):
    def test_from_the_shareholding_filing_and_the_bhavcopy(self):
        d = os.path.join(self.tmp, "fix")
        os.makedirs(d)
        with open(os.path.join(d, "nse_shp_KEC.json"), "w") as fh:
            json.dump([{"date": "30-SEP-2026", "xbrl": "https://x/shp.xml"}], fh)
        with open(os.path.join(d, "shp_KEC.xml"), "w") as fh:
            fh.write('<x><in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="ShareholdingPattern_ContextI" '
                     'unitRef="shares">266200000</in-bse-shp:NumberOfFullyPaidUpEquityShares>'
                     '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="Other">5</in-bse-shp:'
                     'NumberOfFullyPaidUpEquityShares></x>')
        with open(os.path.join(d, "bhav_23112026.csv"), "w") as fh:
            fh.write("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE\n"
                     "KEC, EQ, 23-Nov-2026, 300, 301, 302, 294, 295, 295.25\n")
        mih = im.ih                       # india_market's own india_http (its FeedError, its clock)
        mih.pin_clock("2026-11-24 10:00")
        try:
            notes = []
            cr = im.market_cap_cr(mih.Client(offline=d), "KEC", notes=notes)
        finally:
            mih.pin_clock(None)
        self.assertEqual(cr, 7859.56)                                   # 26.62 cr shares x 295.25


class Pipeline(InTemp):
    def test_orders_latest_keeps_the_window(self):
        d = os.path.join(self.tmp, "fix")
        os.makedirs(d)
        rows = [{"symbol": "KEC", "sm_name": "KEC International Limited", "desc": "Press Release",
                 "attchmntText": "KEC International wins New Orders of Rs. 1,006 crores", "an_dt": "23-Oct-2026 12:48:09",
                 "attchmntFile": "https://x/KEC_1.html"}]
        with open(os.path.join(d, "nse_announcements.json"), "w") as fh:
            json.dump(rows, fh)
        with open(os.path.join(d, "EQUITY_L.csv"), "w") as fh:
            fh.write("SYMBOL,NAME OF COMPANY\nKEC,KEC International Limited\n")
        base = ["--offline", d, "--only", "nse", "--no-pdf"]
        run(orders, base + ["--now", "2026-10-23 13:00"])
        run(orders, base + ["--now", "2026-10-23 13:01"])                # nothing new this pass
        with open(os.path.join("trader", "orders", "latest.json")) as fh:
            doc = json.load(fh)
        self.assertEqual([i["symbol"] for i in doc["items"]], ["KEC"], "the macro engine and the brief still see it")
        self.assertFalse(doc["items"][0]["new"])

    def test_brief_caution_only_for_events_that_passed(self):
        os.makedirs(os.path.join("trader", "macro"))
        with open(os.path.join("trader", "macro", "latest.json"), "w") as fh:
            json.dump({"caution": [{"symbol": "TITAN", "why": "gold up: caution", "gates_1_2": False},
                                   {"symbol": "BPCL", "why": "crude up: caution", "gates_1_2": True}]}, fh)
        code, out, _ = run(brief, ["--now", "2026-10-23 10:41", "--json", "--dry-run", "--offline", self.tmp])
        kinds = {c["symbol"] for c in json.loads(out)["caution"]}
        self.assertEqual(kinds, {"BPCL"})

    def test_macro_entries_get_the_anti_trap_guard(self):
        self.assertIn("midday", brief.anti_trap("12:05"))
        self.assertIn("opening", brief.anti_trap("09:20"))
        self.assertIsNone(brief.anti_trap("10:40"))

    def test_the_fno_swing_short_leg_becomes_a_futures_plan(self):
        r = {"symbol": "KEC", "side": "short", "mode": "intraday", "tags": [im.TAG_INTRADAY_SHORT, im.TAG_FNO_SHORT],
             "fno": {"lot": 1000, "banned": False}, "entry": 100.8, "stop": 101.3, "target1": 100.05,
             "signal_time": "2026-10-23 10:40", "zone": [100.8, 101.0], "origin": "order block 5m",
             "swing": {"p5_pct": -2.1}}
        plan = brief.to_plan(r, datetime.datetime(2026, 10, 23, 10, 41), futures=True)
        self.assertEqual((plan["instrument"], plan["lot_size"], plan["mode"], plan["side"], plan["flat_by"]),
                         ("futures", 1000, "swing", "sell", None))
        self.assertEqual(plan["exit_by"], "2026-10-28")
        with self.assertRaises(brief.PlanError):
            brief.to_plan(dict(r, tags=[im.TAG_INTRADAY_SHORT]), datetime.datetime(2026, 10, 23), futures=True)


class Entries(unittest.TestCase):
    def test_a_played_out_setup_is_expired(self):
        bars = ib.parse(fx.five_minute_chart(), datetime.datetime(2026, 10, 23, 5, 11))
        after = dict(bars[-1], time="10:40", ts=bars[-1]["ts"] + 300, o=100.8, h=100.9, l=99.0, c=99.2, v=1000.0)
        s = [x for x in obs.evaluate("KEC", "5m", bars + [after], daily=fx.daily_bars()) if x["confirmation"]]
        self.assertEqual(s[0]["status"], "expired")
        self.assertIn("the stop traded", s[0]["why_not"][-1])

    def test_short_risk_uses_the_lower_price(self):
        o = {"symbol": "TCS", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "90.00", "stop_loss_stop_price": "102.00",
             "plan": {"entry": 100.0, "stop": 102.0, "target": 90.0, "side": "sell"}}
        cfg = dict(check.DEFAULTS, allow_short=True, max_loss_per_trade_inr=1000, budget=100000)
        res = check.check([o], cfg, prices={"TCS": 99.0}, now=check.parse_now("2026-10-19 10:15"),
                          price_times={"TCS": check.parse_now("2026-10-19 10:14")})
        info = " ".join(res["results"][0]["info"])
        self.assertIn("risk 3.00 a share", info)

    def test_yes_check_reads_long_symbols(self):
        tcs = {"symbol": "TCS", "side": "buy", "qty": "5", "type": "market", "time_in_force": "day"}
        self.assertFalse(ticket.parse_yes("yes RELIANCE", tcs, ["TCS", "RELIANCE"])[0])
        self.assertTrue(ticket.parse_yes("yes TCS", tcs, ["TCS", "RELIANCE"])[0])


class BrokerShorts(InTemp):
    def test_a_short_fill_and_its_cover(self):
        pf = "portfolio.json"
        with self.assertRaises(ticket.TicketError):
            ticket.apply_fill(pf, {"budget": 100000}, "TCS", "sell", 10, 100, "2026-10-19")
        p = ticket.apply_fill(pf, {"budget": 100000, "allow_short": True}, "TCS", "sell", 10, 100, "2026-10-19")
        self.assertEqual(p["positions"][0]["shares"], -10)
        p = ticket.apply_fill(pf, {"allow_short": True}, "TCS", "buy", 10, 98, "2026-10-19")
        self.assertEqual(p["positions"], [])
        self.assertEqual(p["cash"], 100020.0)

    def test_flatten_buys_back_a_short(self):
        with open("portfolio.json", "w") as fh:
            json.dump({"cash": 100000, "positions": [{"ticker": "TCS", "shares": -10, "lots": []}]}, fh)
        code, out, err = run(ticket, ["flatten", "--symbol", "TCS", "--portfolio", "portfolio.json", "--orders", "orders.json",
                                      "--config", "none.json", "--plan", "none.json", "--now", "2026-10-19 15:10"])
        with open("orders.json") as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual((o["side"], o["qty"]), ("buy", "10"), out + err)


if __name__ == "__main__":
    unittest.main()
