"""Step 3 of the audit roadmap: the funnel. Stage scoring, the catalyst direction carried into the entry,
vetoes, the PASS / WAIT / FAIL verdicts, the journal and its review. Offline, on the synthetic KEC chart."""

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

funnel = load("trader-timemachine", "funnel")
NOW = datetime.datetime(2026, 10, 23, 10, 41)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = funnel.main(argv)
    return code, out.getvalue(), err.getvalue()


class NoOdds(object):
    def __init__(self, verdicts=None):
        self.v = verdicts or {}

    def verdict(self, etype, side):
        return self.v.get((etype, side))


def order(kind="award", impact=12.8, value=1006, sym="KEC", t="2026-10-23 10:28"):
    return {"id": "NSE|%s|%s" % (sym, kind), "symbol": sym, "time": t, "kind": kind, "source": "NSE announcements",
            "event_type": {"award": "order_win", "cancel": "order_cancel"}.get(kind), "title": "%s: order" % sym,
            "link": "https://x/%s-%s.pdf" % (sym, kind), "numbers": {"value_cr": value, "impact_pct": impact},
            "flags": [], "tag": "orders", "tags": ["orders"]}


class Stages(unittest.TestCase):
    def test_catalyst_points_and_direction(self):
        ev_ = funnel.evidence_for("KEC", [order()], [], {}, NOW)
        pts, side, why = funnel.score_catalyst(ev_, NoOdds())
        self.assertEqual((pts, side), (25, "long"))
        self.assertIn("12.8% of market cap", why[0])
        pts, side, _ = funnel.score_catalyst(ev_, NoOdds({("order_win", 1): "edge"}))
        self.assertEqual(pts, 35, "an out-of-sample edge in the event study adds 10")
        pts, _, _ = funnel.score_catalyst(ev_, NoOdds({("order_win", 1): "failed out of sample"}))
        self.assertEqual(pts, 20)
        both = funnel.evidence_for("KEC", [order(), order("cancel", 12.8)], [], {}, NOW)
        self.assertEqual(funnel.score_catalyst(both, NoOdds())[1], None, "25 vs 25: no direction")
        self.assertEqual(funnel.score_catalyst([], NoOdds()), (0, None, ["no catalyst in the last 2 days"]))

    def test_smart_money_and_macro_evidence(self):
        sm = {"kind": "smart_money", "symbol": "KEC", "time": "2026-10-23 09:00", "title": "promoter sold", "detail": "",
              "flags": ["promoter", "sale"], "link": "l1", "highlight": False, "tags": [], "numbers": {},
              "category": "insider", "source": "NSE PIT", "company": ""}
        macro = {"actionable_long": [{"symbol": "KEC", "label": "Crude", "direction": "down"}]}
        e = funnel.evidence_for("KEC", [], [sm], macro, NOW)
        self.assertEqual(sorted((x["side"], x["points"]) for x in e), [(-1, 10), (1, 10)])

    def test_universe_vetoes(self):
        bhav = {"KEC": {"close": 300, "turnover_cr": 60.0, "series": "EQ"},
                "THIN": {"close": 50, "turnover_cr": 2.0, "series": "EQ"},
                "TFT": {"close": 50, "turnover_cr": 40.0, "series": "BE"}}
        pts, vetoes, notes, _ = funnel.stage_universe("KEC", bhav, None, [])
        self.assertEqual((pts, vetoes), (8, []))
        self.assertIn("illiquid", funnel.stage_universe("THIN", bhav, None, [])[1][0])
        self.assertIn("trade-for-trade", funnel.stage_universe("TFT", bhav, None, [])[1][0])
        flags = [{"flag": "surveillance", "why": "added to ASM"}, {"flag": "price band 5%", "why": "x"},
                 {"flag": "results due 2026-10-23", "why": "board meeting tonight"}]
        _, vetoes, _, overnight = funnel.stage_universe("KEC", bhav, None, flags)
        self.assertEqual(len(vetoes), 2)
        self.assertEqual(overnight, ["board meeting tonight"], "results tonight only stops an overnight hold")
        self.assertEqual(funnel.regime_points({"regime": "risk-off"}, "long"), 0)
        self.assertEqual(funnel.regime_points({"regime": "risk-off"}, "short"), 10)

    def test_structure_follows_the_catalyst(self):
        long_entry = {"symbol": "KEC", "tf": "5m", "side": "long", "status": "entry", "confirmation": "absorption",
                      "vwap_ok": True, "vwap_kind": "session VWAP", "oi": None, "guards": [], "why_not": []}
        self.assertEqual(funnel.score_structure([long_entry], "long")[:3], (20, long_entry, "entry"))
        pts, best, status, why = funnel.score_structure([long_entry], "short")
        self.assertEqual((pts, best, status), (0, None, "wait"))
        self.assertIn("points the other way", why[0])
        blocked = dict(long_entry, status="blocked", guards=["blocked: midday fakeouts (11:45-13:30)"])
        self.assertEqual(funnel.score_structure([blocked], "long")[2], "veto")

    def test_time_machine_points(self):
        row = {"mode": "intraday", "intraday": {"expectancy_R": 0.3}, "swing": None}
        self.assertEqual(funnel.score_tm(row)[:2], (15, None))
        self.assertEqual(funnel.score_tm(dict(row, intraday={"expectancy_R": 0.1}))[:2], (5, None))
        self.assertIn("no edge after costs", funnel.score_tm(dict(row, intraday={"expectancy_R": -0.2}))[1])
        sw = {"mode": "swing", "swing": {"expectancy_R": 0.4, "windows": 12}, "intraday": None}
        self.assertIn("too few past windows", funnel.score_tm(sw)[1])


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.fix = fx.write(os.path.join(self.tmp, "fix"))
        os.makedirs(os.path.join("trader", "orders"))
        os.makedirs(os.path.join("trader", "regime"))
        with open(os.path.join("trader", "regime", "latest.json"), "w") as fh:
            json.dump({"regime": "risk-on", "score": 3, "of": 5, "volatility": "normal", "size_hint": "full",
                       "advice": "x", "checked": "2026-10-23 08:45"}, fh)
        self.real_build = funnel.brief.build

    def tearDown(self):
        funnel.brief.build = self.real_build
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def orders(self, *items):
        with open(os.path.join("trader", "orders", "latest.json"), "w") as fh:
            json.dump({"items": list(items)}, fh)

    def go(self, *extra):
        code, out, err = run(["--offline", self.fix, "--now", "2026-10-23 10:41", "--sims", "1000", "--tf", "5m",
                              "--json"] + list(extra))
        return code, json.loads(out)

    def test_a_big_order_with_a_confirmed_entry_but_no_edge_fails_at_the_time_machine(self):
        self.orders(order())
        code, doc = self.go()
        r = doc["rows"][0]
        self.assertEqual((r["symbol"], r["side"], r["verdict"]), ("KEC", "long", "FAIL"))
        self.assertEqual((r["scores"]["catalyst"], r["scores"]["structure"]), (25, 20))
        self.assertEqual(r["vetoes"][0][0], "timemachine")
        self.assertEqual(doc["counts"], {"candidates": 1, "after_universe": 1, "with_direction": 1, "with_entry": 1,
                                         "passed": 0})
        self.assertEqual(code, 1)
        with open(os.path.join("trader", "journal", "signals.csv")) as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 2, "the header and one signal")

    def test_pass_when_every_stage_agrees(self):
        self.orders(order())

        def fake(args, notes, cands=None):
            c = cands[0]
            return {"active": [{"symbol": c["symbol"], "side": c["side"], "mode": "intraday", "tf": c["tf"],
                                "entry": c["entry"], "stop": c["stop"], "target1": c["target1"], "zone": c["zone"],
                                "tags": ["[EQUITY INTRADAY LONG]"], "swing": None,
                                "intraday": {"expectancy_R": 0.35, "replays": 1000, "sessions": 25, "win_pct": 55,
                                             "p_target": 30, "p_stop": 20, "p_open_at_flat": 50, "rr": 1.5,
                                             "mae_R": {"median": 0.5, "p95": 1.0}}}],
                    "caution": [], "regime": None}
        funnel.brief.build = fake
        code, doc = self.go()
        r = doc["rows"][0]
        self.assertEqual((r["verdict"], r["total"]), ("PASS", 12 + 25 + 20 + 15))
        self.assertEqual(code, 0)

    def test_a_cancellation_waits_for_a_short_structure(self):
        self.orders(order("cancel"))
        _, doc = self.go("--dry-run")
        r = doc["rows"][0]
        self.assertEqual((r["side"], r["verdict"]), ("short", "WAIT"))
        self.assertIn("points the other way", r["why"]["structure"][0])

    def test_review_scores_what_happened(self):
        self.orders(order())
        self.go()
        path = os.path.join(self.fix, "KEC.NS_1d.json")
        with open(path) as fh:
            daily = json.load(fh)
        for d, c in (("2026-10-23", 101.5), ("2026-10-26", 103.0), ("2026-10-27", 102.0)):
            daily.append({"date": d, "time": "", "o": c, "h": c + 0.3, "l": c - 0.3, "c": c, "v": 1.0})
        with open(path, "w") as fh:
            json.dump(daily, fh)
        code, out, _ = run(["review", "--offline", self.fix, "--now", "2026-10-28 18:00"])
        self.assertEqual(code, 0)
        self.assertIn("verdict FAIL", out)
        self.assertIn("vetoed at timemachine", out)
        with open(os.path.join("trader", "journal", "outcomes.csv")) as fh:
            rows = fh.read().splitlines()
        self.assertIn("0.694", rows[1], "the signal day closed 101.50 against the 100.80 entry")


if __name__ == "__main__":
    unittest.main()
