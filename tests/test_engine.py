"""The catalyst engine: government orders and execution risk, the global macro gates, order blocks with
anti-trap guards, and the universal Time Machine brief. Offline: synthetic charts (engine_fixtures.py)
and hand-written feed answers; no network."""

import datetime
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT
import engine_fixtures as fx

orders = load("trader-news", "govt_orders_tracker")
im = load("trader-news", "india_market")
ic = load("trader-news", "india_catalysts")
ib = load("trader-scanner", "india_bars")
obs = load("trader-scanner", "order_block_scanner")
macro = load("trader-scanner", "global_macro_engine")
tm = load("trader-timemachine", "timemachine")
ev = load("trader-timemachine", "events")
brief = load("trader-timemachine", "brief")
ih = load("trader-news", "india_http")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class InTemp(unittest.TestCase):
    """Run in a scratch folder: the tools write trader/... relative to the working directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)
        ih.pin_clock(None)


# ---------------------------------------------------------------- 1. government orders

class OrderText(unittest.TestCase):
    def test_kinds(self):
        cases = [
            ("KEC International wins New Orders of Rs. 1,006 crores", "award"),
            ("Rail Vikas Nigam emerges as L1 bidder for Rs 554 crore project", "award"),
            ("MSEDCL has cancelled the Letter of Award issued to the company", "cancel"),
            ("Termination of contract awarded by NHAI", "cancel"),
            ("Delay in execution of the project; liquidated damages levied by the client", "delay"),
            ("Company debarred by Ministry of Railways for two years", "debar"),
            ("Successfully commissioned the 50 MW solar plant and handed over to the client", "milestone"),
            ("Receipt of order under Section 73 of the CGST Act, demand of Rs 2 crore", None),
            ("Order passed by the Hon'ble High Court", None),
        ]
        for text, want in cases:
            self.assertEqual(orders.classify(text)[0], want, text)

    def test_values(self):
        self.assertEqual(orders.order_value_cr("wins New Orders of Rs. 1,006 crores"), 1006.0)
        self.assertEqual(orders.order_value_cr("order book of Rs 40,000 crore; new order worth Rs 554 crore"), 554.0)
        self.assertEqual(orders.order_value_cr("contract valued at USD 25 million"), 212.5)
        self.assertEqual(orders.order_value_cr("a total order value of ₹4.72 million"), 0.47)
        self.assertEqual(orders.order_value_cr("aggregating to Rs. 4,52,00,000 (excluding GST)"), 4.52)
        self.assertEqual(orders.order_value_cr("an order of Rs 12.5 lakh"), 0.12)
        self.assertIsNone(orders.order_value_cr("revenue of Rs 900 crore"))

    def test_timeline_and_due_date(self):
        self.assertEqual(orders.timeline_months("to be executed within 24 months"), 24.0)
        self.assertEqual(orders.timeline_months("completed in 18 (eighteen) months"), 18.0)
        self.assertEqual(orders.timeline_months("over a period of three years"), 36.0)
        self.assertEqual(orders.due_date("to be completed by March 2028", "2026-11-23 10:00"), "2028-03-31")
        self.assertEqual(orders.due_date("by FY28", "2026-11-23 10:00"), "2028-03-31")
        self.assertEqual(orders.due_date("within 12 months", "2026-01-01 10:00"), "2027-01-01")

    def test_client_is_never_the_company_itself(self):
        it = orders.make_item("NSE", "RVNL: Bagging", "Rail Vikas Nigam Limited has received a Letter of Award from "
                              "NMDC Limited worth Rs 300 crore", "2026-11-23 23:08", "x.pdf", symbol="RVNL",
                              company="Rail Vikas Nigam Limited")
        self.assertEqual(it["client"], "PSU / central government")
        it = orders.make_item("NSE", "X: order", "Letter of Award from MSEDCL worth Rs 80 crore", "2026-11-23 10:00",
                              "x.pdf", symbol="ABC", company="Abc Ltd")
        self.assertEqual(it["client"], "MSEDCL / Maharashtra")

    def test_impact_and_high_impact_flag(self):
        it = orders.make_item("NSE", "X: order", "Letter of Award worth Rs 600 crore, to be executed within 24 months",
                              "2026-11-23 10:00", "x.pdf", symbol="ABC", mcap=lambda s: 8000.0)
        self.assertEqual(it["numbers"]["impact_pct"], 7.5)
        self.assertIn("HIGH IMPACT", it["flags"])
        self.assertEqual(it["numbers"]["timeline_months"], 24.0)
        small = orders.make_item("NSE", "X: order", "work order worth Rs 60 crore", "2026-11-23 10:00", "y.pdf",
                                 symbol="ABC", mcap=lambda s: 8000.0)
        self.assertNotIn("HIGH IMPACT", small["flags"])

    def test_rss_dates_in_gmt_are_converted_to_india_time(self):
        self.assertEqual(ih.parse_when("Fri, 23 Oct 2026 05:05:00 GMT"), "2026-10-23 10:35")
        self.assertEqual(ih.parse_when("Fri, 23 Oct 2026 10:35:00 +0530"), "2026-10-23 10:35")

    def test_results_from_the_press(self):
        self.assertEqual(orders.results_change("net profit rose 34% to Rs 120 crore; revenue declined 5 per cent"),
                         {"profit_yoy": 34.0, "revenue_yoy": -5.0})

    def test_name_matcher(self):
        master = {"KEC": "KEC International Limited", "RVNL": "Rail Vikas Nigam Limited", "BEL": "Bharat Electronics Limited",
                  "POWERGRID": "Power Grid Corporation of India Limited", "INDIA": "India Limited", "LT": "Larsen & Toubro Limited"}
        nm = im.NameMatcher(master, {"L&T": "LT"})
        self.assertEqual(nm.find("Rail Vikas Nigam emerges L1"), [("RVNL", "name")])
        self.assertEqual(nm.find("Bharat Electronics gets defence order"), [("BEL", "name")])
        self.assertEqual(nm.find("Power Grid wins transmission project"), [("POWERGRID", "name")])
        self.assertEqual(nm.find("India power demand rises"), [])
        self.assertEqual(nm.find("L&T bags mega order"), [("LT", "symbol")])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(orders.SCHEMA)

    def tearDown(self):
        self.con.close()

    def add(self, text, when, sym="ABC", link=None):
        it = orders.make_item("NSE", "%s: %s" % (sym, text[:40]), text, when, link or "%s.pdf" % when, symbol=sym)
        orders.store(self.con, [it], when)
        return it

    def test_execution_lag_watch(self):
        self.add("Letter of Award worth Rs 500 crore from MSEDCL, to be executed within 10 months", "2026-01-05 10:00")
        self.assertEqual(orders.ledger_flags(self.con, "2026-05-01"), [])             # 39 % of the way
        flags = orders.ledger_flags(self.con, "2026-08-01")                           # 68 %
        self.assertEqual([f["flag"] for f in flags], ["EXECUTION LAG WATCH"])
        self.assertGreaterEqual(flags[0]["elapsed_pct"], 65)
        self.add("Successfully commissioned the substation and handed over to the client", "2026-07-20 10:00")
        self.assertEqual(orders.ledger_flags(self.con, "2026-08-01"), [])

    def test_order_loss_matched_on_value(self):
        self.add("Letter of Award worth Rs 500 crore from NHAI", "2026-03-01 10:00")
        self.add("work order worth Rs 80 crore from MSEDCL", "2026-04-01 10:00")
        self.add("NHAI has terminated the contract worth Rs 510 crore", "2026-06-01 10:00")
        f = [x for x in orders.ledger_flags(self.con, "2026-06-02") if x["flag"] == "ORDER LOSS / CANCELLATION"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["matched_by"], "value")
        self.assertIn("500", f[0]["order"])

    def test_debarment(self):
        self.add("The company has been debarred by the Ministry of Defence", "2026-06-01 10:00", sym="XYZ")
        self.assertEqual([f["flag"] for f in orders.ledger_flags(self.con, "2026-06-02")], ["DEBARMENT"])


class OrderCollector(InTemp):
    def fixtures(self):
        d = os.path.join(self.tmp, "fix")
        os.makedirs(d)
        rows = [{"symbol": "KEC", "sm_name": "KEC International Limited", "desc": "Press Release",
                 "attchmntText": "KEC International wins New Orders of Rs. 1,006 crores", "an_dt": "23-Oct-2026 12:48:09",
                 "attchmntFile": "https://nsearchives.nseindia.in/corporate/KEC_1.html"},
                {"symbol": "ZZZ", "sm_name": "Zzz Limited", "desc": "Updates",
                 "attchmntText": "Receipt of order under Section 73 of the CGST Act", "an_dt": "23-Oct-2026 11:00:00",
                 "attchmntFile": "https://x/zzz.pdf"}]
        with open(os.path.join(d, "nse_announcements.json"), "w") as fh:
            json.dump(rows, fh)
        with open(os.path.join(d, "EQUITY_L.csv"), "w") as fh:
            fh.write("SYMBOL,NAME OF COMPANY, SERIES\nKEC,KEC International Limited,EQ\nRVNL,Rail Vikas Nigam Limited,EQ\n")
        with open(os.path.join(d, "nse_quote_KEC.json"), "w") as fh:
            json.dump({"priceInfo": {"lastPrice": 800.0}, "securityInfo": {"issuedSize": 257083654}}, fh)
        with open(os.path.join(d, "nse_quote_RVNL.json"), "w") as fh:
            json.dump({"priceInfo": {"lastPrice": 180.0}, "securityInfo": {"issuedSize": 2085020100}}, fh)
        rss = ('<?xml version="1.0"?><rss><channel><item><title>Rail Vikas Nigam emerges L1 bidder for Rs 554 crore '
               'East Coast Railway project - Economic Times</title><link>https://news.example/a</link>'
               '<pubDate>Fri, 23 Oct 2026 07:10:00 GMT</pubDate><source url="https://economictimes.indiatimes.com">'
               'Economic Times</source></item><item><title>Markets end flat - Mint</title><link>https://news.example/b</link>'
               '<pubDate>Fri, 23 Oct 2026 07:20:00 GMT</pubDate></item></channel></rss>')
        with open(os.path.join(d, ih.safe_name(orders.gnews_url('"L1 bidder"', 1))), "w") as fh:
            fh.write(rss)
        with open(os.path.join(d, ih.safe_name("https://bidplus.gem.gov.in/all-bids")), "w") as fh:
            fh.write("<html><div class='g-recaptcha'></div>Please solve the captcha</html>")
        with open(os.path.join("feeds.json"), "w") as fh:
            json.dump({"gnews_queries": ['"L1 bidder"'], "gem": ["https://bidplus.gem.gov.in/all-bids"]}, fh)
        return d

    def test_offline_pass(self):
        d = self.fixtures()
        code, out, err = run(orders, ["--offline", d, "--now", "2026-10-23 13:30", "--only", "nse,gnews,gem",
                                      "--feeds", "feeds.json", "--no-pdf", "--json"])
        doc = json.loads(out)
        by = {i["symbol"]: i for i in doc["items"]}
        self.assertEqual(set(by), {"KEC", "RVNL"}, "the GST order is not a contract; 'Markets end flat' names no company")
        self.assertEqual(by["KEC"]["numbers"]["value_cr"], 1006.0)
        self.assertEqual(by["KEC"]["numbers"]["mcap_cr"], 20566.69)
        self.assertEqual(by["RVNL"]["outlet"], "Economic Times")
        self.assertEqual(by["RVNL"]["client"], "Railways")
        self.assertTrue(any("CAPTCHA" in n for n in doc["notes"]))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join("trader", "orders", "latest.json")))
        self.assertTrue(os.path.exists(os.path.join("trader", "history", "orders.db")))
        code, out, _ = run(orders, ["--offline", d, "--now", "2026-10-23 13:30", "--only", "nse", "--no-pdf"])
        self.assertEqual(code, 1, "the second pass has nothing new")

    def test_now_hides_later_filings(self):
        d = self.fixtures()
        _, out, _ = run(orders, ["--offline", d, "--now", "2026-10-23 12:00", "--only", "nse", "--no-pdf", "--json",
                                 "--dry-run"])
        self.assertEqual(json.loads(out)["items"], [])

    def test_watch_loop_stops_after_max_passes(self):
        d = self.fixtures()
        code, out, _ = run(orders, ["--offline", d, "--now", "2026-10-23 13:30", "--only", "nse", "--no-pdf",
                                    "--watch", "1", "--max-passes", "1", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("KEC", out)

    def test_india_signals_sees_order_flags(self):
        sig = load("trader-entry", "india_signals")
        os.makedirs(os.path.join("trader", "orders"))
        with open(os.path.join("trader", "orders", "latest.json"), "w") as fh:
            json.dump({"items": [{"id": "x", "symbol": "ABC", "time": "2026-10-23 10:00", "tag": "orders", "tags": ["orders"],
                                  "title": "ABC ORDER LOSS / CANCELLATION", "detail": "", "link": "l",
                                  "flags": ["ORDER LOSS / CANCELLATION"], "numbers": {}}]}, fh)
        now = datetime.datetime(2026, 10, 23, 11, 0)
        flags = sig.risk_flags(sig.signals_for("ABC", now), now)
        self.assertEqual([f["flag"] for f in flags], ["order loss / cancellation"])


# ---------------------------------------------------------------- 2. macro gates and routing

def art(t, domain, title="Gold prices surge to a record", **kw):
    return dict({"time": t, "domain": domain, "title": title, "link": "https://%s/x" % domain}, **kw)


class MacroGates(unittest.TestCase):
    def test_gate1_tier1_or_two_outlets_in_15_minutes(self):
        ok, why, t = macro.gate1([art("2026-11-24 10:00", "rbi.org.in")], ["rbi.org.in"])
        self.assertTrue(ok)
        self.assertIn("tier-1", why)
        ok, why, t = macro.gate1([art("2026-11-24 10:00", "a.com"), art("2026-11-24 10:12", "b.com")], [])
        self.assertTrue(ok)
        self.assertEqual(t, "2026-11-24 10:12")
        ok, why, _ = macro.gate1([art("2026-11-24 10:00", "a.com"), art("2026-11-24 10:20", "b.com")], [])
        self.assertFalse(ok, "20 minutes apart is not confirmation")
        ok, _, _ = macro.gate1([art("2026-11-24 10:00", "a.com"), art("2026-11-24 10:05", "a.com")], [])
        self.assertFalse(ok, "the same outlet twice is one source")

    def test_gate2_threshold_and_direction(self):
        th = {"proxies": [{"symbol": "GC=F", "threshold_pct": 1.5, "sign": 1}]}
        self.assertTrue(macro.gate2_macro(th, "up", {"GC=F": 1.8})[0])
        self.assertFalse(macro.gate2_macro(th, "up", {"GC=F": 1.2})[0])
        self.assertFalse(macro.gate2_macro(th, "up", {"GC=F": -2.0})[0], "a fall does not confirm a surge")
        self.assertTrue(macro.gate2_macro(th, "down", {"GC=F": -2.0})[0])
        self.assertFalse(macro.gate2_macro(th, "up", {"GC=F": None})[0])

    def test_theme_direction(self):
        mp, _ = macro.load_map()
        hits = macro.theme_hits([art("2026-11-24 10:00", "a.com", "Gold prices surge to a record high"),
                                 art("2026-11-24 10:01", "b.com", "Crude oil prices plunge on output hike"),
                                 art("2026-11-24 10:02", "c.com", "Ceasefire and truce agreed after border clash")], mp["themes"])
        self.assertIn(("gold", "up"), hits)
        self.assertIn(("crude", "down"), hits)
        self.assertIn(("war", "down"), hits)

    def test_default_matrix_has_the_examples(self):
        mp, _ = macro.load_map()
        self.assertEqual(mp["themes"]["gold"]["up"]["long"][:3], ["MUTHOOTFIN", "MANAPPURAM", "TITAN"])
        war = mp["themes"]["war"]["up"]
        self.assertTrue({"HAL", "BEL", "MAZDOCK", "ONGC"} <= set(war["long"]))
        self.assertEqual(set(war["short"]), {"INDIGO", "ASIANPAINT", "BPCL", "HPCL"})

    def test_gate3_window(self):
        day = "2026-11-23"
        bars = []
        for k in range(40):
            m = 9 * 60 + 15 + 5 * k
            bars.append({"date": day, "time": "%02d:%02d" % divmod(m, 60), "o": 100, "h": 100.2, "l": 99.8,
                         "c": 100.0, "v": 1000.0})
        start = datetime.datetime(2026, 11, 23, 10, 0)
        cfg = {"min_wait_minutes": 5, "max_wait_minutes": 15, "volume_ratio": 2.0, "bars": 20}
        self.assertEqual(macro.gate3(bars, start, datetime.datetime(2026, 11, 23, 10, 3), "long", cfg)["status"], "pending")
        self.assertEqual(macro.gate3(bars, start, datetime.datetime(2026, 11, 23, 10, 30), "long", cfg)["status"], "dropped")
        b = [dict(x) for x in bars]
        i = next(j for j, x in enumerate(b) if x["time"] == "10:05")      # ends 10:10: inside 10:05-10:15
        b[i].update(c=100.6, h=100.7, v=3000.0)
        g = macro.gate3(b, start, datetime.datetime(2026, 11, 23, 10, 12), "long", cfg)
        self.assertEqual(g["status"], "confirmed")
        self.assertEqual(g["volume_ratio"], 3.0)
        b[i].update(c=99.4, l=99.3)
        self.assertEqual(macro.gate3(b, start, datetime.datetime(2026, 11, 23, 10, 30), "long", cfg)["status"],
                         "wrong way")

    def test_news_outside_hours_starts_at_the_next_open(self):
        self.assertEqual(macro.effective_start("2026-11-23 21:30"), datetime.datetime(2026, 11, 25, 9, 15),
                         "24 Nov 2026 is an NSE holiday")
        self.assertEqual(macro.effective_start("2026-11-23 11:02"), datetime.datetime(2026, 11, 23, 11, 2))

    def test_routing_follows_sebi_cash_rules(self):
        self.assertEqual(im.route("long"), ["[EQUITY INTRADAY LONG]"])
        self.assertEqual(im.route("long", swing=True), ["[EQUITY SWING LONG: 2-3+ DAYS]"])
        self.assertEqual(im.route("short", swing=True), ["[EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM]"],
                         "a cash short is never held overnight")
        self.assertEqual(im.route("short", fno_listed=True, multi_day_bearish=False), [im.TAG_INTRADAY_SHORT])
        self.assertEqual(im.route("short", fno_listed=True, multi_day_bearish=True),
                         [im.TAG_INTRADAY_SHORT, "[SECONDARY F&O SWING SHORT OPTION]"])
        self.assertEqual(im.route("short", fno_listed=True, fno_banned=True, multi_day_bearish=True),
                         [im.TAG_INTRADAY_SHORT])


class MacroRun(InTemp):
    def test_company_event_passes_all_gates(self):
        fix = fx.write(os.path.join(self.tmp, "fix"))
        os.makedirs(os.path.join("trader", "orders"))
        with open(os.path.join("trader", "orders", "latest.json"), "w") as fh:
            json.dump({"items": [{"symbol": "KEC", "time": "2026-10-23 10:35", "kind": "award", "source": "NSE announcements",
                                  "title": "KEC: order", "link": "https://nsearchives.nseindia.in/k.pdf",
                                  "numbers": {"impact_pct": 6.2}}]}, fh)
        with open("map.json", "w") as fh:
            json.dump({"themes": {}, "feeds": [], "tier1_domains": [], "gate3": {}, "company_events": {}}, fh)
        code, out, _ = run(macro, ["--map", "map.json", "--offline", fix, "--now", "2026-10-23 10:41", "--no-gdelt",
                                   "--no-fno", "--json", "--dry-run"])
        doc = json.loads(out)
        ev0 = doc["events"][0]
        self.assertTrue(ev0["gate1"]["ok"] and ev0["gate2"]["ok"])
        self.assertEqual([r["symbol"] for r in doc["actionable_long"]], ["KEC"])
        self.assertEqual(doc["actionable_long"][0]["gate3"]["status"], "confirmed")
        self.assertEqual(code, 0)


    def test_gold_theme_end_to_end(self):
        fix = fx.write(os.path.join(self.tmp, "fix"), symbol="MUTHOOTFIN")
        mp, _ = macro.load_map()
        gold = dict(mp["themes"]["gold"], up={"long": ["MUTHOOTFIN"], "short": [], "caution": ["TITAN"]})
        feed = "https://feeds.example/markets.xml"
        with open("map.json", "w") as fh:
            json.dump({"themes": {"gold": gold}, "feeds": [feed], "tier1_domains": ["rbi.org.in"],
                       "gate3": mp["gate3"], "company_events": {}}, fh)
        gd = macro.GDELT % (macro.urllib.parse.quote(gold["gdelt"]), "artlist", "3h")
        with open(os.path.join(fix, ih.safe_name(gd)), "w") as fh:     # GDELT seendate is UTC: 10:30 IST
            json.dump({"articles": [{"url": "https://a.example/g", "title": "Gold prices surge to a record",
                                     "seendate": "20261023T050000Z", "domain": "a.example"}]}, fh)
        with open(os.path.join(fix, ih.safe_name(feed)), "w") as fh:
            fh.write('<?xml version="1.0"?><rss><channel><item><title>Gold rallies to fresh high as dollar slips</title>'
                     '<link>https://b.example/g</link><pubDate>Fri, 23 Oct 2026 05:05:00 GMT</pubDate></item>'
                     '</channel></rss>')
        rows = []
        for d, c in (("2026-10-22", 100.0), ("2026-10-23", 102.0)):
            t = int((datetime.datetime.fromisoformat(d + " 04:00") - datetime.datetime(1970, 1, 1)).total_seconds())
            rows.append((t, c, c, c, c, 0))
        with open(os.path.join(fix, "GC_F_5m.json"), "w") as fh:
            json.dump(fx._chart("GC=F", rows), fh)
        code, out, _ = run(macro, ["--map", "map.json", "--offline", fix, "--now", "2026-10-23 10:41", "--no-fno",
                                   "--json", "--dry-run"])
        doc = json.loads(out)
        self.assertEqual(doc["proxy_moves"]["GC=F"], 2.0)
        e = doc["events"][0]
        self.assertEqual((e["theme"], e["direction"]), ("gold", "up"))
        self.assertTrue(e["gate1"]["ok"], e["gate1"]["why"])
        self.assertIn("a.example and b.example within 15 minutes", e["gate1"]["why"])
        self.assertTrue(e["gate2"]["ok"])
        self.assertEqual([r["symbol"] for r in doc["actionable_long"]], ["MUTHOOTFIN"])
        r = doc["actionable_long"][0]
        self.assertEqual(r["gate3"]["status"], "confirmed")
        self.assertEqual(r["tags"], ["[EQUITY SWING LONG: 2-3+ DAYS]"], "gold is a persistent theme")
        self.assertEqual([c["symbol"] for c in doc["caution"]], ["TITAN"])
        self.assertEqual(code, 0)

    def test_gdelt_plain_api_and_time(self):
        self.assertEqual(macro.gdelt_time("20261023T050000Z"), "2026-10-23 10:30")


# ---------------------------------------------------------------- 3. order blocks

def fixture_bars(now="10:41"):
    rows = ib.parse(fx.five_minute_chart(), datetime.datetime(2026, 10, 23, 5, 11))
    return rows


class OrderBlocks(unittest.TestCase):
    def setUp(self):
        self.bars = fixture_bars()
        self.daily = [b for b in fx.daily_bars()]

    def test_finds_the_bullish_order_block(self):
        obs_ = [o for o in obs.find_order_blocks(self.bars) if o["date"] == "2026-10-23"]
        self.assertEqual(len(obs_), 1)
        ob = obs_[0]
        self.assertEqual((ob["side"], ob["time"], ob["low"], ob["high"], ob["mid"]), ("bull", "10:00", 99.7, 100.3, 100.0))
        self.assertEqual(ob["fvg"], [100.3, 100.8])   # the OB high to the 10:10 low
        self.assertEqual(ob["bos_level"], 100.6)
        self.assertGreaterEqual(ob["impulse_atr"], 2.0)
        self.assertEqual(ob["status"], "tapped")

    def test_sweep_and_reclaim_gives_an_entry(self):
        s = obs.evaluate("KEC", "5m", self.bars, daily=self.daily)
        e = [x for x in s if x["status"] == "entry"]
        self.assertEqual(len(e), 1)
        e = e[0]
        self.assertEqual((e["confirmation"], e["signal_time"], e["entry"]), ("sweep and reclaim", "2026-10-23 10:40", 100.8))
        self.assertGreaterEqual(e["volume_ratio"], 1.8)
        self.assertTrue(e["vwap_ok"])
        self.assertLess(e["stop"], 99.7)
        self.assertEqual(e["mean_threshold"], 100.0)

    def test_time_guards(self):
        self.assertIn("opening noise", obs.time_guard("09:25"))
        self.assertIn("midday", obs.time_guard("12:10"))
        self.assertIn("no_entries_after", obs.time_guard("15:00"))
        self.assertIsNone(obs.time_guard("10:40"))
        self.assertIsNone(obs.time_guard("13:35"))

    def test_extension_guard(self):
        tight = [dict(b, o=100.0, c=100.0, h=100.05, l=99.95) for b in self.daily]      # a tiny daily ATR
        s = [x for x in obs.evaluate("KEC", "5m", self.bars, daily=tight) if x["confirmation"]]
        self.assertEqual(s[0]["status"], "blocked")
        self.assertTrue(any("14-day ATR" in g for g in s[0]["guards"]))

    def test_no_confirmation_without_volume(self):
        quiet = [dict(b) for b in self.bars]
        quiet[-1]["v"] = 1000.0
        s = obs.evaluate("KEC", "5m", quiet, daily=self.daily)
        self.assertTrue(s)
        self.assertEqual(s[0]["status"], "waiting")
        self.assertIn("no sweep", s[0]["why_not"][0])

    def test_bearish_mirror(self):
        flip = [dict(b, o=200 - b["o"], c=200 - b["c"], h=200 - b["l"], l=200 - b["h"]) for b in self.bars]
        s = [x for x in obs.evaluate("KEC", "5m", flip, daily=[dict(d, o=200 - d["o"], c=200 - d["c"], h=200 - d["l"],
                                                                   l=200 - d["h"]) for d in self.daily])
             if x["status"] == "entry"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["side"], "short")
        self.assertGreater(s[0]["stop"], 100.3)

    def test_absorption_candle(self):
        ob = {"side": "bull", "low": 99.0, "high": 101.0, "atr": 1.0}
        bars = [{"o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 1000.0} for _ in range(20)]
        bars.append({"o": 99.9, "h": 100.25, "l": 99.85, "c": 100.2, "v": 3000.0})
        self.assertEqual(obs.confirmation(bars, ob, 20)[0], "absorption")

    def test_resample_and_vwap(self):
        b15 = ib.resample([b for b in self.bars if b["date"] == "2026-10-23"], 15)
        self.assertEqual([b["time"] for b in b15][:3], ["09:15", "09:30", "09:45"])
        self.assertEqual(b15[3]["l"], 99.7)
        self.assertAlmostEqual(ib.vwap([{"h": 2, "l": 2, "c": 2, "v": 1}, {"h": 4, "l": 4, "c": 4, "v": 3}]), 3.5)

    def test_daily_trend(self):
        down = [{"o": 100 - i * 0.3, "h": 101 - i * 0.3 + (0.8 if i % 4 == 0 else 0), "l": 99 - i * 0.3,
                 "c": 100 - i * 0.3, "v": 1, "date": "d%03d" % i} for i in range(40)]
        self.assertEqual(obs.daily_trend(down), "bearish")


# ---------------------------------------------------------------- 4. the Time Machine and the brief

class Simulations(unittest.TestCase):
    def test_intraday_mae_and_exit_window(self):
        pool = [[(1.0, 1.002, 0.998, 1.001)] for _ in range(tm.SESSION_BARS)]
        mc = tm.simulate(pool, 100.0, 99.0, 102.0, 10, 74, replays=200, seed=1, window=0)
        self.assertIn("mae_R", mc)
        self.assertEqual(len(mc["avg_R_by_bar"]), 64)
        self.assertGreater(mc["avg_R_by_bar"][-1], mc["avg_R_by_bar"][0], "a steady drift up pays later")
        self.assertLessEqual(mc["mae_R"]["p95"], 1.0)

    def test_swing_paths(self):
        up = [[(1.01, 1.03, 1.005, 1.02), (1.02, 1.05, 1.015, 1.04), (1.04, 1.06, 1.03, 1.05)]] * 30
        r = ev.swing_paths(up, 100, 97, 104, 1, 3, sims=500)
        self.assertEqual(r["win_pct"], 100.0)
        self.assertEqual(r["outcomes"], {"target": 100.0})
        self.assertEqual(r["best_exit_day"], 2)
        gap = [[(0.95, 0.96, 0.94, 0.95), (1, 1, 1, 1), (1, 1, 1, 1)]] * 30
        r = ev.swing_paths(gap, 100, 97, 104, 1, 3, sims=500)
        self.assertEqual(r["outcomes"], {"stop gap": 100.0})
        self.assertAlmostEqual(r["mean_net_pct"], -5.0 - ev.COST, places=3, msg="the gap's loss, not the stop's, after costs")
        self.assertAlmostEqual(r["mae_R"]["p95"], 5 / 3, places=3)
        r = ev.swing_paths(gap, 100, 103, 96, -1, 3, sims=200)
        self.assertEqual(r["outcomes"], {"target gap": 100.0}, "the same gap is a short's target")

    def test_windows_from_bars(self):
        bars = [{"o": 100, "h": 101, "l": 99, "c": 100}, {"o": 102, "h": 103, "l": 101, "c": 102},
                {"o": 102, "h": 104, "l": 100, "c": 103}]
        self.assertEqual(ev.windows_from_bars(bars, 1)[0], [(1.02, 1.03, 1.01, 1.02)])


class BriefEndToEnd(InTemp):
    def chain(self, now="2026-10-23 10:41"):
        fix = fx.write(os.path.join(self.tmp, "fix"))
        code, out, err = run(obs, ["--symbols", "KEC", "--offline", fix, "--now", now, "--no-oi", "--tf", "5m,15m,1d"])
        self.assertEqual(code, 0, out + err)
        code, out, err = run(brief, ["--offline", fix, "--now", now, "--sims", "1500", "--json"])
        return code, json.loads(out), fix

    def test_every_setup_is_simulated(self):
        code, doc, _ = self.chain()
        self.assertEqual(code, 0)
        self.assertEqual(len(doc["active"]), 1)
        r = doc["active"][0]
        self.assertEqual((r["symbol"], r["label"], r["tags"]), ("KEC", "[EQUITY LONG]", ["[EQUITY INTRADAY LONG]"]))
        self.assertEqual(r["zone"], [99.7, 100.3])
        self.assertEqual(r["mean_threshold"], 100.0)
        for k in ("win_pct", "expectancy_R", "rr", "mae_R", "best_exit"):
            self.assertIn(k, r["intraday"], k)
        self.assertEqual(r["intraday"]["sessions"], 25)
        for k in ("win_pct", "expectancy_R", "mae_R", "best_exit_day", "avg_net_by_day", "gap_through_stop_pct"):
            self.assertIn(k, r["swing"], k)
        self.assertTrue(r["exit_window"]["best"])
        self.assertEqual(r["edge"], r["intraday"]["expectancy_R"] > 0)
        self.assertTrue(os.path.exists(os.path.join("trader", "brief", "latest.md")))

    def test_plan_from_the_brief(self):
        self.chain()
        code, out, _ = run(brief, ["plan", "1", "--write"])
        self.assertEqual(code, 0)
        with open(os.path.join("trader", "plan.json")) as fh:
            plan = json.load(fh)
        self.assertEqual((plan["symbol"], plan["side"], plan["mode"], plan["flat_by"], plan["entry_time"]),
                         ("KEC", "buy", "day", "15:15", "10:40"))
        self.assertLess(plan["stop"], plan["entry"])

    def test_the_risk_check_keeps_the_anti_trap_windows(self):
        size = load("trader-budget", "size")
        check = load("trader-risk", "check")
        self.chain()
        run(brief, ["plan", "1", "--write"])
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        cfg.update(max_loss_per_trade_inr=500, max_daily_loss_inr=1500)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)
        code, out, err = run(size, ["--now", "2026-10-23 10:41", "--write"])
        self.assertEqual(code, 0, out + err)
        _, out, _ = run(check, ["--now", "2026-10-23 10:41", "--today", "2026-10-23", "--no-write"])
        self.assertIn("PASS", out)
        _, out, _ = run(check, ["--now", "2026-10-23 12:10", "--today", "2026-10-23", "--no-write"])
        self.assertIn("BLOCK", out)
        self.assertIn("midday fakeouts 11:45-13:30", out)

    def test_blocked_setups_go_to_caution(self):
        fix = fx.write(os.path.join(self.tmp, "fix"))
        os.makedirs(os.path.join("trader", "orderblocks"))
        setup = {"symbol": "KEC", "tf": "5m", "mode": "intraday", "side": "long", "status": "blocked",
                 "guards": ["blocked: midday fakeouts (11:45-13:30)"], "why_not": []}
        with open(os.path.join("trader", "orderblocks", "latest.json"), "w") as fh:
            json.dump({"setups": [setup]}, fh)
        os.makedirs(os.path.join("trader", "orders"))
        with open(os.path.join("trader", "orders", "latest.json"), "w") as fh:
            json.dump({"items": [], "watch": [{"flag": "EXECUTION LAG WATCH", "symbol": "ABC", "title": "t",
                                               "why": "70% of the timeline has passed", "link": "l"}]}, fh)
        code, out, _ = run(brief, ["--offline", fix, "--now", "2026-10-23 12:00", "--json", "--dry-run"])
        doc = json.loads(out)
        kinds = {(c["symbol"], c["kind"]) for c in doc["caution"]}
        self.assertIn(("KEC", "retail trap guard"), kinds)
        self.assertIn(("ABC", "EXECUTION LAG WATCH"), kinds)
        self.assertEqual(doc["active"], [])
        self.assertEqual(code, 1)

    def test_a_short_is_intraday_in_cash_and_fno_only_when_eligible(self):
        fix = fx.write(os.path.join(self.tmp, "fix"))
        with open(os.path.join(fix, "fo_mktlots.csv"), "w") as fh:
            fh.write("UNDERLYING,SYMBOL,NOV-26\nKec Intl,KEC,1000\n")
        with open(os.path.join(fix, "fo_secban.csv"), "w") as fh:
            fh.write("Securities in Ban For Trade Date 23-OCT-2026:\n")
        os.makedirs(os.path.join("trader", "orderblocks"))
        base = {"symbol": "KEC", "tf": "5m", "mode": "intraday", "side": "short", "status": "entry",
                "zone": [100.8, 101.0], "mean_threshold": 100.9, "entry": 100.8, "stop": 101.3, "target1": 100.05,
                "target2": 99.3, "rr1": 1.5, "signal_time": "2026-10-23 10:40", "confirmation": "absorption",
                "guards": [], "why_not": []}
        for trend, want in (("mixed", ["[EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM]"]),
                            ("bearish", ["[EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15 PM]"])):
            with open(os.path.join("trader", "orderblocks", "latest.json"), "w") as fh:
                json.dump({"setups": [dict(base, daily_trend=trend)]}, fh)
            _, out, _ = run(brief, ["--offline", fix, "--now", "2026-10-23 10:41", "--sims", "500", "--json", "--dry-run"])
            r = json.loads(out)["active"][0]
            self.assertEqual(r["tags"], want, "an intraday short has no multi-day setup: no F&O tag")
            self.assertIsNone(r["swing"], "a cash short is never replayed overnight")
        with open(os.path.join("trader", "orderblocks", "latest.json"), "w") as fh:
            json.dump({"setups": [dict(base, tf="1d", mode="swing", daily_trend="bearish",
                                       signal_time="2026-10-22 ")]}, fh)
        _, out, _ = run(brief, ["--offline", fix, "--now", "2026-10-23 10:41", "--sims", "500", "--json", "--dry-run"])
        r = json.loads(out)["active"][0]
        self.assertEqual(r["tags"], [im.TAG_INTRADAY_SHORT, "[SECONDARY F&O SWING SHORT OPTION]"])
        self.assertIn("win_pct", r["swing"], "the F&O swing short is replayed with overnight gaps")


if __name__ == "__main__":
    unittest.main()
