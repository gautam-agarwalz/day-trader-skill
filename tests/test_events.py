"""events.py: the NSE event history, the event study, the rule replays and today's ideas.

The parsers run on small NSE-shaped answers; the study and the suggestions run on a synthetic
database whose outcome is known (a stock that always gaps up the morning after an order win)."""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from _load import load

ev = load("trader-timemachine", "events")
ic = ev.ic

BHAV_HEAD = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, "
             "TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n")


def bhav(day, rows):
    d = datetime.date.fromisoformat(day).strftime("%d-%b-%Y")
    return BHAV_HEAD + "".join("%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1000, %s, 10, 500, 50.00\n" % (
        s, ser, d, pc, o, h, lo, c, c, c, t) for s, ser, pc, o, h, lo, c, t in rows)


class Classify(unittest.TestCase):
    def test_event_types_and_directions(self):
        cases = [
            ("Board Meeting Intimation", "XYZ Ltd has informed the Exchange about Board Meeting to be held on "
             "08-Oct-2026 to consider Buyback of equity shares", "board_meeting_buyback", "up"),
            ("Spurt in Volume", "Significant increase in volume has been observed in ITI Limited.", "price_query", "?"),
            ("Bagging/Receiving of orders/contracts", "Texmaco has informed the Exchange about Bagging/Receiving of orders",
             "order_win", "up"),
            ("Resignation of Statutory Auditor", "Arham has informed the Exchange about Resignation of Statutory Auditor",
             "auditor_resignation", "down"),
            ("Rumour Verification - Regulation 30(11)", "Clarification on News Article", "media_clarification", "?"),
            ("Outcome of Board Meeting", "KSS Limited has submitted to the Exchange, the financial results for the "
             "period ended September 30, 2026.", "results", "?"),
            ("Credit Rating- Revision", "Edelweiss has informed the Exchange about upgrade in Credit Rating", "rating_up", "up"),
            ("Press Release", "KEC International wins New Orders of Rs. 1,006 crores", "order_win", "up"),
            ("Press Release", "B. L. Kashyap has secured a work order of Rs. 49 crores", "order_win", "up"),
            ("Updates", "XYZ received a demand notice for orders under section 7 of IBC", "default", "down"),
        ]
        for desc, text, want, d in cases:
            self.assertEqual(ic.event_type(text, desc), (want, d), desc)
        self.assertEqual(ic.event_type("Copy of newspaper publication", "Copy of Newspaper Publication"), (None, None))

    def test_outcome_of_a_meeting_is_not_an_early_signal(self):
        t, _ = ic.event_type("The Board considered and approved the buyback of shares", "Outcome of Board Meeting")
        self.assertEqual(t, "buyback")

    def test_meeting_date(self):
        self.assertEqual(ic.meeting_date("Board Meeting to be held on 08-Oct-2026 to consider"), "2026-10-08")
        self.assertEqual(ic.meeting_date("meeting to be held on Monday, 3rd November 2025"), "2025-11-03")
        self.assertIsNone(ic.meeting_date("no date here"))

    def test_families(self):
        self.assertEqual(ic.family_of("ceo_change"), "management")
        self.assertEqual(ic.family_of("bulk_net_buy"), "smart_money")
        self.assertIn("band_cut", ic.ALL_TYPES)


class Parsers(unittest.TestCase):
    def test_bhavcopy(self):
        day, rows = ev.parse_bhavcopy(bhav("2026-10-01", [("ABC", "EQ", 100, 101, 103, 99, 102, 250),
                                                          ("ABCSM", "SM", 10, 10, 10, 10, 10, 1),
                                                          ("XYZ", "BE", 50, 50, 51, 49, 50.5, 20)]))
        self.assertEqual(day, "2026-10-01")
        self.assertEqual([r["symbol"] for r in rows], ["ABC", "XYZ"])
        self.assertEqual(rows[0]["turnover_cr"], 2.5)
        self.assertEqual(rows[0]["deliv"], 50.0)

    def test_announcements(self):
        doc = [{"symbol": "ABC", "sm_name": "Abc Ltd", "desc": "Bagging/Receiving of orders/contracts",
                "attchmntText": "Abc Ltd has informed the Exchange about Bagging/Receiving of orders",
                "sort_date": "2026-10-05 13:05:10", "seq_id": "1", "attchmntFile": "https://x/a.pdf"},
               {"symbol": "ABC", "desc": "Copy of Newspaper Publication", "attchmntText": "newspaper",
                "sort_date": "2026-10-05 13:05:10", "seq_id": "2"}]
        evs = ev.parse_announcements(doc)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["type"], evs[0]["time"], evs[0]["id"]), ("order_win", "2026-10-05 13:05", "ann|1"))

    def test_meetings(self):
        doc = [{"bm_symbol": "DCAL", "bm_date": "07-Oct-2026", "bm_purpose": "Fund Raising",
                "bm_desc": "Board Meeting to be held on 07-Oct-2026 to consider Fund raising",
                "bm_timestamp": "02-Oct-2026 17:27:16", "attachment": "https://x/b.xml"},
               {"bm_symbol": "TCS", "bm_date": "08-Oct-2026", "bm_purpose": "Financial Results",
                "bm_desc": "To consider and approve the financial results", "bm_timestamp": "20-Sep-2026 10:00:00"}]
        evs = ev.parse_meetings(doc)
        self.assertEqual([(e["symbol"], e["type"], e["event_date"], e["time"]) for e in evs],
                         [("DCAL", "board_meeting_fundraise", "2026-10-07", "2026-10-02 17:27"),
                          ("TCS", "board_meeting_results", "2026-10-08", "2026-09-20 10:00")])

    def test_bands(self):
        text = "Sr. No,Symbol,Series,Security Name,From,To\n1,ABC,EQ,ABC LTD,10,5\n2,XYZ,EQ,XYZ,5,10\n3,SMS,SM,S,20,5\n"
        evs = ev.parse_bands(text, "2026-10-01")
        self.assertEqual([(e["symbol"], e["type"]) for e in evs], [("ABC", "band_cut"), ("XYZ", "band_widen")])
        self.assertEqual(evs[0]["time"], "2026-10-01 18:00")

    def test_deals_drop_round_trips(self):
        bulk = {"data": [
            {"BD_SYMBOL": "ABC", "BD_CLIENT_NAME": "FUND A", "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 100000, "BD_TP_WATP": 500},
            {"BD_SYMBOL": "ABC", "BD_CLIENT_NAME": "TRADER", "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 900000, "BD_TP_WATP": 500},
            {"BD_SYMBOL": "ABC", "BD_CLIENT_NAME": "TRADER", "BD_BUY_SELL": "SELL", "BD_QTY_TRD": 900000, "BD_TP_WATP": 501},
            {"BD_SYMBOL": "XYZ", "BD_CLIENT_NAME": "FUND B", "BD_BUY_SELL": "SELL", "BD_QTY_TRD": 10000, "BD_TP_WATP": 50}]}
        block = {"data": [{"BD_SYMBOL": "LMN", "BD_CLIENT_NAME": "GS", "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 1000000,
                           "BD_TP_WATP": 700}]}
        evs = {e["symbol"]: e for e in ev.parse_deals(block, bulk, "2026-10-01")}
        self.assertEqual(evs["ABC"]["type"], "bulk_net_buy")
        self.assertAlmostEqual(evs["ABC"]["value_cr"], 5.0)
        self.assertNotIn("XYZ", evs)                      # Rs 0.05 cr: too small
        self.assertEqual((evs["LMN"]["type"], evs["LMN"]["value_cr"]), ("block_deal", 70.0))


class Replay(unittest.TestCase):
    def test_gap_through_the_stop_fills_at_the_open(self):
        r, how = ev.simulate([(0.90, 0.95, 0.88, 0.93)], 1.0, 0.05, 0.10)
        self.assertEqual(how, "stop gap")
        self.assertAlmostEqual(r, -0.10)

    def test_stop_and_target_in_one_bar_count_as_the_stop(self):
        self.assertEqual(ev.simulate([(1.0, 1.2, 0.9, 1.0)], 1.0, 0.05, 0.10), (-0.05, "stop"))

    def test_target_time_and_short(self):
        self.assertEqual(ev.simulate([(1.01, 1.12, 1.0, 1.1)], 1.0, 0.05, 0.10), (0.10, "target"))
        r, how = ev.simulate([(1.0, 1.02, 0.99, 1.01), (1.01, 1.03, 1.0, 1.02)], 1.0, 0.05, 0.10)
        self.assertEqual(how, "time")
        self.assertAlmostEqual(r, 0.02)
        self.assertEqual(ev.simulate([(1.0, 1.01, 0.85, 0.9)], 1.0, 0.05, 0.10, side=-1), (0.10, "target"))
        r, how = ev.simulate([(1.03, 1.04, 1.0, 1.0)], 1.0, None, None, exit_at_open=True)
        self.assertEqual(how, "open")
        self.assertAlmostEqual(r, 0.03)

    def test_bootstrap_is_seeded(self):
        a = ev.bootstrap([1.0, -0.5, 0.3, 0.8, -0.2] * 8, sims=500)
        b = ev.bootstrap([1.0, -0.5, 0.3, 0.8, -0.2] * 8, sims=500)
        self.assertEqual(a, b)
        self.assertLess(a["mean_p5"], a["mean_p95"])


def build_db(path, n_events=40, gap=0.02, start=datetime.date(2024, 1, 1), every=3):
    """A database where ABC.. stocks gap up `gap` the morning after every order win filed at 13:00
    (one every `every` sessions; 9 spreads them over 1.5 years, so the last 12 months can be checked)."""
    con = ev.connect(path)
    days, d = [], start
    while len(days) < 30 + n_events * every:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    prices = {}
    events = []
    for i, day in enumerate(days):
        rows = []
        for s in ("ABC", "XYZ", "THIN"):
            pc = prices.get(s, 100.0)
            o, c = pc, pc
            k = i - 30
            if s == "ABC" and k > 0 and k % every == 1:     # the morning after an event day: gap up, hold it
                o = c = pc * (1 + gap)
            if s == "XYZ" and k > 0 and k % every == 1:     # and XYZ gaps down after its auditor resigns
                o = c = pc * (1 - gap)
            h, lo = max(o, c) * 1.01, min(o, c) * 0.99
            rows.append({"symbol": s, "series": "EQ", "open": o, "high": h, "low": lo, "close": c, "prev_close": pc,
                         "volume": 1000.0, "turnover_cr": 0.1 if s == "THIN" else 5.0, "deliv": 50.0})
            prices[s] = c
        ev.add_bars(con, day, rows)
        ev.mark_loaded(con, "bars", day, len(rows))
        k = i - 30
        if k >= 0 and k % every == 0:
            events.append({"id": "ann|%d" % i, "symbol": "ABC", "time": day + " 13:00", "type": "order_win",
                           "direction": "up", "text": "order", "link": "l%d" % i})
            events.append({"id": "aud|%d" % i, "symbol": "XYZ", "time": day + " 13:00", "type": "auditor_resignation",
                           "direction": "down", "text": "auditor", "link": "a%d" % i})
            events.append({"id": "thin|%d" % i, "symbol": "THIN", "time": day + " 13:00", "type": "order_win",
                           "direction": "up", "text": "order", "link": "t%d" % i})
    ev.add_events(con, events)
    con.commit()
    return con, days


class Study(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con, self.days = build_db(os.path.join(self.tmp, "e.sqlite"))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp)

    def test_session_and_day0(self):
        cal = ev.Calendar(self.con)
        d0, d1 = self.days[40], self.days[41]
        self.assertEqual(cal.session(d0 + " 08:00"), ("pre", d0))
        self.assertEqual(cal.session(d0 + " 13:00"), ("during", d0))
        self.assertEqual(cal.session(d0 + " 16:10"), ("after", d1))
        sat = datetime.date.fromisoformat(d0)
        while sat.weekday() != 5:
            sat += datetime.timedelta(days=1)
        self.assertEqual(cal.session(sat.isoformat() + " 11:00")[0], "after")

    def test_order_wins_gap_up_and_the_study_sees_it(self):
        out, events = ev.study(self.con, "order_win", sims=400)
        s = out["summary"]
        self.assertTrue(all(e["symbol"] == "ABC" for e in events), "the illiquid stock is left out")
        self.assertGreaterEqual(s["n"], 30)
        self.assertAlmostEqual(s["overnight"]["mean"], 2.0, places=3)
        o = out["rules"]["overnight"]["in_sample"]
        self.assertAlmostEqual(o["mean"], 2.0 - ev.COST, places=3)
        self.assertEqual(o["win"], 100.0)
        best = out["rules"]["best"]
        self.assertTrue(best["verdict"].startswith("edge"))
        self.assertEqual(best["rule"]["entry"], "close0")
        self.assertGreater(best["in_sample"]["bootstrap"]["p_edge"], 99)

    def test_no_look_ahead_when_replaying_a_past_day(self):
        cut = self.days[60] + " 13:30"
        out, events = ev.study(self.con, "order_win", sims=100, before=cut)
        self.assertTrue(events)
        self.assertTrue(all(e["time"] < cut for e in events))
        self.assertTrue(all(len(e["path"]) <= 30 for e in events))
        last = max(d for e in events for d in [e["time"][:10]])
        self.assertLess(last, self.days[60])
        self.assertLess(out["split"], self.days[60])
        full, _ = ev.study(self.con, "order_win", sims=100)
        self.assertGreater(full["summary"]["n"], out["summary"]["n"])

    def test_too_few_events_say_so(self):
        out, _ = ev.study(self.con, "rating_up", sims=100)
        self.assertEqual(out["summary"]["n"], 0)
        self.assertIn("too few", out["note"])

    def test_print_study(self):
        import io
        out, events = ev.study(self.con, "order_win", sims=200)
        buf = io.StringIO()
        ev.print_study(out, events, buf)
        text = buf.getvalue()
        self.assertIn("overnight (close -> open)", text)
        self.assertIn("bootstrap resamples", text)
        self.assertIn("History, not a forecast", text)


class Suggest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con, self.days = build_db(os.path.join(self.tmp, "e.sqlite"), n_events=70, every=9)
        self.today = self.days[-1]

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp)

    def items(self, when):
        base = {"kind": "catalyst", "company": "", "tags": [], "category": "", "numbers": {}, "event_date": "",
                "highlight": False, "source": "NSE announcements"}
        return [dict(base, id="1", symbol="ABC", time=when, title="ABC: Bagging/Receiving of orders/contracts",
                     detail="Abc has informed the Exchange about Bagging/Receiving of orders",
                     flags=["Bagging/Receiving of orders/contracts"], link="https://x/abc.pdf"),
                dict(base, id="2", symbol="THIN", time=when, title="THIN: Bagging/Receiving of orders/contracts",
                     detail="Thin has informed the Exchange about Bagging/Receiving of orders",
                     flags=["Bagging/Receiving of orders/contracts"], link="https://x/thin.pdf"),
                dict(base, id="3", symbol="ZZZ", time=when, title="ZZZ: Copy of Newspaper Publication",
                     detail="newspaper", flags=["Copy of Newspaper Publication"], link="")]

    def test_a_filing_during_the_session_gives_a_buy_at_the_close_idea(self):
        now = datetime.datetime.fromisoformat(self.today + " 14:30")
        doc = ev.suggest(self.con, now, self.items(self.today + " 13:10"), quotes={"ABC": {"price": 101, "prev_close": 100}},
                         sims=300)
        self.assertEqual(doc["session"], "during")
        self.assertEqual([d["symbol"] for d in doc["ideas"]], ["ABC"])
        idea = doc["ideas"][0]
        self.assertEqual(idea["type"], "order_win")
        self.assertIn("15:10-15:25 on %s" % self.today, idea["entry"])
        self.assertEqual(idea["reference_price"], 101)
        self.assertGreater(idea["average_net_pct"], 1.0)
        self.assertTrue(any("THIN" in s and "illiquid" in s for s in doc["skipped"]))

    def test_after_the_close_the_gap_cannot_be_bought(self):
        now = datetime.datetime.fromisoformat(self.today + " 16:00")
        doc = ev.suggest(self.con, now, self.items(self.today + " 13:10"), sims=200)
        self.assertEqual(doc["session"], "after")
        for d in doc["ideas"]:
            self.assertIn("buy at the open", d["entry"])
        for d in doc["warnings"] + doc["ideas"]:
            self.assertNotIn("15:10", d.get("entry", ""))

    def test_entries_still_open(self):
        cal = ev.Calendar(self.con)
        d0 = self.days[-3]
        at = lambda t: datetime.datetime.fromisoformat(d0 + " " + t)  # noqa: E731
        self.assertEqual(ev.open_entries("pre", d0, at("08:00"), cal), ("open0", "close0", "open1"))
        self.assertEqual(ev.open_entries("during", d0, at("13:00"), cal), ("close0", "open1"))
        self.assertEqual(ev.open_entries("during", d0, at("16:00"), cal), ("open1",))
        self.assertEqual(ev.open_entries("during", self.days[-5], at("16:00"), cal), ())
        self.assertEqual(ev.live_session(d0 + " 16:00", cal), ("after", self.days[-2]))

    def test_old_items_are_ignored(self):
        now = datetime.datetime.fromisoformat(self.today + " 14:30")
        doc = ev.suggest(self.con, now, self.items(self.days[-5] + " 13:10"), sims=100)
        self.assertEqual(doc["fresh_items"], 0)
        self.assertEqual(doc["ideas"], [])

    def test_cli_types_study_and_suggest(self):
        import contextlib
        import io
        db = os.path.join(self.tmp, "e.sqlite")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ev.main(["--db", db, "types"]), 0)
            self.assertEqual(ev.main(["--db", db, "study", "--type", "order_win", "--sims", "200"]), 0)
        self.assertIn("order_win", buf.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ev.main(["--db", db, "study", "--type", "nonsense"]), 2)
        base = os.path.join(self.tmp, "trader")
        os.makedirs(os.path.join(base, "catalysts"))
        rows = [dict(i, tags=[]) for i in self.items(self.today + " 13:10")]
        with open(os.path.join(base, "catalysts", "latest.json"), "w") as fh:
            json.dump({"items": rows}, fh)
        quotes = os.path.join(self.tmp, "q.json")
        with open(quotes, "w") as fh:
            json.dump({"ABC": {"price": 100.5, "prev_close": 100}}, fh)
        out = os.path.join(self.tmp, "ideas")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ev.main(["--db", db, "--now", self.today + " 14:30", "suggest", "--base", base, "--out", out,
                          "--quotes", quotes, "--sims", "200"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("ABC  order_win", buf.getvalue())
        with open(os.path.join(out, "latest.json")) as fh:
            self.assertEqual(json.load(fh)["ideas"][0]["symbol"], "ABC")
        self.assertTrue(os.path.exists(os.path.join(out, "ideas.csv")))


class Shorts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con, self.days = build_db(os.path.join(self.tmp, "e.sqlite"), n_events=70, every=9)
        self.today = self.days[-1]

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp)

    def item(self, when):
        return {"kind": "catalyst", "company": "", "tags": [], "category": "", "numbers": {}, "event_date": "",
                "highlight": False, "source": "NSE announcements", "id": "9", "symbol": "XYZ", "time": when,
                "title": "XYZ: Resignation of Statutory Auditor",
                "detail": "Xyz has informed the Exchange about Resignation of Statutory Auditor",
                "flags": ["Resignation of Statutory Auditor"], "link": "https://x/xyz.pdf"}

    def test_lots_and_ban_parsers(self):
        lots = ev.parse_lots("UNDERLYING ,SYMBOL ,DEC-26 ,JAN-27\nNIFTY 50 ,NIFTY ,65 ,65\n"
                             "Xyz Ltd ,XYZ ,  ,500\nSymbol ,SYMBOL ,x ,y\n")
        self.assertEqual(lots, {"NIFTY": 65, "XYZ": 500})
        day, banned = ev.parse_ban("Securities in Ban For Trade Date 25-NOV-2026:\n1,MANAPPURAM\n2,XYZ\n")
        self.assertEqual((day, banned), ("2026-11-25", ["MANAPPURAM", "XYZ"]))

    def test_a_bearish_filing_is_a_warning_without_futures(self):
        now = datetime.datetime.fromisoformat(self.today + " 14:00")
        doc = ev.suggest(self.con, now, [self.item(self.today + " 13:00")], sims=200)
        self.assertEqual(doc["ideas"], [])
        self.assertEqual(doc["warnings"][0]["symbol"], "XYZ")
        self.assertIn("the F&O list is not loaded", doc["warnings"][0]["why"])
        self.con.execute("INSERT INTO fno VALUES ('RELIANCE', 500, ?)", (self.today,))
        doc = ev.suggest(self.con, now, [self.item(self.today + " 13:00")], sims=200)
        self.assertIn("not an F&O stock", doc["warnings"][0]["why"])

    def test_an_edge_never_checked_on_the_last_12_months_is_not_suggested(self):
        tmp = tempfile.mkdtemp()
        try:
            con, days = build_db(os.path.join(tmp, "short.sqlite"))      # about 7 months: nothing to check against
            now = datetime.datetime.fromisoformat(days[-1] + " 14:30")
            item = dict(self.item(days[-1] + " 13:10"), symbol="ABC", title="ABC: Bagging/Receiving of orders/contracts",
                        detail="Abc has informed the Exchange about Bagging/Receiving of orders",
                        flags=["Bagging/Receiving of orders/contracts"])
            doc = ev.suggest(con, now, [item], sims=100)
            self.assertEqual(doc["ideas"], [])
            self.assertTrue(any("too few trades in the last 12 months" in x for x in doc["skipped"]), doc["skipped"])
            con.close()
        finally:
            shutil.rmtree(tmp)

    def test_an_fno_stock_gets_an_overnight_futures_short(self):
        self.con.execute("INSERT INTO fno VALUES ('XYZ', 500, ?)", (self.today,))
        now = datetime.datetime.fromisoformat(self.today + " 14:00")
        doc = ev.suggest(self.con, now, [self.item(self.today + " 13:00")], sims=200)
        idea = doc["ideas"][0]
        self.assertEqual((idea["symbol"], idea["side"], idea["instrument"], idea["lot_size"]),
                         ("XYZ", "short", "futures", 500))
        self.assertIn("short (sell first) 15:10-15:25", idea["entry"])
        self.assertGreater(idea["average_net_pct"], 1.0)
        plan = ev.plan_from_idea(idea, 1.0)
        self.assertEqual((plan["side"], plan["instrument"], plan["lot_size"], plan["mode"]),
                         ("sell", "futures", 500, "swing"))
        self.assertGreater(plan["stop"], plan["entry"])
        self.assertLess(plan["target"], plan["entry"])

    def test_a_banned_stock_gets_no_futures_short(self):
        self.con.execute("INSERT INTO fno VALUES ('XYZ', 500, ?)", (self.today,))
        self.con.execute("INSERT INTO fno_ban VALUES (?, 'XYZ')", (self.today,))
        now = datetime.datetime.fromisoformat(self.today + " 14:00")
        doc = ev.suggest(self.con, now, [self.item(self.today + " 13:00")], sims=100)
        self.assertEqual(doc["ideas"], [])
        self.assertTrue(any("ban period" in x for x in doc["skipped"]))

    def test_study_short_side(self):
        out, _ = ev.study(self.con, "auditor_resignation", side=-1, sims=200)
        best = out["rules"]["best"]
        self.assertEqual(best["side"], "short")
        self.assertGreater(best["in_sample"]["mean"], 1.0)
        self.assertIn("short (sell first)", best["text"])


class Backfill(unittest.TestCase):
    def test_offline_backfill_and_holiday(self):
        tmp = tempfile.mkdtemp()
        try:
            fx = os.path.join(tmp, "fx")
            os.makedirs(fx)
            with open(os.path.join(fx, "bhav_01102026.csv"), "w") as fh:
                fh.write(bhav("2026-10-01", [("ABC", "EQ", 100, 101, 103, 99, 102, 250)]))
            with open(os.path.join(fx, "bhav_02102026.csv"), "w") as fh:    # a holiday: yesterday's file again
                fh.write(bhav("2026-10-01", [("ABC", "EQ", 100, 101, 103, 99, 102, 250)]))
            with open(os.path.join(fx, "bands_01102026.csv"), "w") as fh:
                fh.write("Sr. No,Symbol,Series,Security Name,From,To\n1,ABC,EQ,ABC LTD,20,10\n")
            with open(os.path.join(fx, "deals_block_2026-10-01.json"), "w") as fh:
                json.dump({"data": []}, fh)
            with open(os.path.join(fx, "deals_bulk_2026-10-01.json"), "w") as fh:
                json.dump({"data": []}, fh)
            with open(os.path.join(fx, "ann_2026-09-28.json"), "w") as fh:
                json.dump([{"symbol": "ABC", "desc": "Buyback", "attchmntText": "Abc has informed the Exchange about Buyback",
                            "sort_date": "2026-10-01 11:00:00", "seq_id": "9"}], fh)
            for wk in ("2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"):
                with open(os.path.join(fx, "meetings_%s.json" % wk), "w") as fh:
                    json.dump([], fh)
            con = ev.connect(os.path.join(tmp, "e.sqlite"))
            client = ev.ih.Client(offline=fx)
            logs = []
            stats = ev.backfill(con, client, datetime.date(2026, 10, 1), datetime.date(2026, 10, 2), log=logs.append,
                                today=datetime.date(2026, 11, 23))
            self.assertEqual((stats["days"], stats["holidays"]), (1, 1))
            types = dict(con.execute("SELECT type, count(*) FROM events GROUP BY type").fetchall())
            self.assertEqual(types, {"band_cut": 1, "buyback": 1})
            self.assertTrue(ev.is_loaded(con, "bars", "2026-10-02"))
            again = ev.backfill(con, client, datetime.date(2026, 10, 1), datetime.date(2026, 10, 2), log=logs.append,
                                today=datetime.date(2026, 11, 23))
            fetched = client.calls[-again["calls"]:]
            self.assertFalse([u for u in fetched if "bhavdata" in u or "band_changes" in u or "deals" in u],
                             "a second run fetches no day it already has (only the unfinished weeks)")
            con.close()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()


class EventPool(unittest.TestCase):
    """The time machine's event-day pool: other stocks' day-0 sessions of the same event type."""

    def setUp(self):
        import gzip
        from _load import FIX
        self.tm = load("trader-timemachine", "timemachine")
        with gzip.open(os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz"), "rt", encoding="utf-8") as fh:
            self.text = fh.read()
        self.now = self.tm.parse_now("2026-10-19 10:35")
        _, bars, _ = self.tm.parse_bars(self.tm.chart_result(self.text, "TSLA"), self.now)
        days = self.tm.split_sessions(bars)
        self.full = [d for d in sorted(days) if len(days[d]) == self.tm.SESSION_BARS and d < "2026-10-19"]
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "e.sqlite")
        con = ev.connect(self.db)
        for d in self.full:
            ev.add_bars(con, d, [{"symbol": "X", "series": "EQ", "open": 1, "high": 1, "low": 1, "close": 1,
                                  "prev_close": 1, "volume": 1, "turnover_cr": 5, "deliv": 1}])
        evs = [{"id": "e%d" % i, "symbol": "S%02d" % i, "time": d + " 11:00", "type": "order_win", "direction": "up"}
               for i, d in enumerate(self.full[-14:])]
        evs.append({"id": "own", "symbol": "TSLA", "time": self.full[-1] + " 11:00", "type": "order_win",
                    "direction": "up"})
        ev.add_events(con, evs)
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pool_of_other_stocks_event_days(self):
        sessions, info = self.tm.event_pool("order_win", "2026-10-19", lambda s: self.text, self.now, self.db,
                                            exclude="TSLA")
        self.assertEqual((len(sessions), info["stocks"], info["used"]), (14, 14, "order_win"))
        self.assertTrue(all("TSLA" not in lbl for lbl, _ in sessions))
        plan = self.tm.normalize_plan({"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb",
                                       "entry": 364.18, "entry_time": "10:00 AM", "entry_bar": 8, "stop": 358.32,
                                       "target": 370.04})
        doc = self.tm.time_machine(self.text, plan, self.now, replays=500, event_sessions=sessions, event_info=info)
        self.assertEqual((doc["mc"]["pool_kind"], doc["mc"]["pool_sessions"]), ("event days", 14))
        self.assertIn("event days", doc["note"])
        self.assertIn("event days of other stocks", "\n".join(self.tm.report(doc)))

    def test_too_few_event_days_fall_back_to_own_days(self):
        sessions, info = self.tm.event_pool("order_win", "2026-10-19", lambda s: self.text, self.now, self.db, limit=5)
        plan = self.tm.normalize_plan({"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb",
                                       "entry": 364.18, "entry_time": "10:00 AM", "entry_bar": 8, "stop": 358.32,
                                       "target": 370.04})
        doc = self.tm.time_machine(self.text, plan, self.now, replays=200, event_sessions=sessions, event_info=info)
        self.assertEqual(doc["mc"]["pool_kind"], "own days")
        self.assertTrue(any("event-day sessions" in w for w in doc["warnings"]))

    def test_no_history_is_said(self):
        with self.assertRaises(self.tm.TMError):
            self.tm.event_pool("order_win", "2026-10-19", lambda s: self.text, self.now,
                               os.path.join(self.tmp, "none.sqlite"))
