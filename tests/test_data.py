"""Step 2 of the audit roadmap: data completeness. Relative volume by time slot, the market regime, and a
backfill that loads filings first and stops asking a failing deals service. Offline."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX
from test_events import bhav

ib = load("trader-scanner", "india_bars")
macro = load("trader-scanner", "global_macro_engine")
regime = load("trader-scanner", "regime")
ev = load("trader-timemachine", "events")
brief = load("trader-timemachine", "brief")

REGIME_FIX = os.path.join(FIX, "trader-scanner", "regime")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def sessions(days, open_vol=5000.0, rest_vol=1000.0, last_open=None, last_move=0.0):
    """5-minute bars: opening slots (09:15-09:25) heavy every day, the rest light. The last session can set its
    own opening volume and move."""
    out = []
    d = datetime.date(2026, 10, 1)
    made = 0
    while made < days:
        if d.weekday() < 5:
            last = made == days - 1
            for k in range(75 if not last else 6):
                m = 9 * 60 + 15 + 5 * k
                heavy = k < 3
                v = (last_open if (last and heavy and last_open) else open_vol if heavy else rest_vol)
                c = 100.0 + (last_move if last and k >= 1 else 0.0)
                out.append({"date": d.isoformat(), "time": "%02d:%02d" % divmod(m, 60), "o": 100.0, "h": c + 0.1,
                            "l": 99.9, "c": c, "v": v})
            made += 1
        d += datetime.timedelta(days=1)
    return out


class SlotVolume(unittest.TestCase):
    def test_the_open_is_compared_with_other_opens(self):
        bars = sessions(8)
        i = len(bars) - 5                                  # the last session's 09:20 bar
        self.assertEqual(bars[i]["time"], "09:20")
        self.assertGreaterEqual(ib.vol_ratio(bars, i), 2.0, "the old measure: 5x yesterday's afternoon")
        self.assertEqual(ib.slot_rvol(bars, i), 1.0, "the same slot on earlier days: an ordinary open")
        self.assertEqual(ib.rvol(bars, i)[1], "same slot, last 20 sessions")

    def test_too_little_history_falls_back(self):
        bars = sessions(3)
        self.assertIsNone(ib.slot_rvol(bars, len(bars) - 1))
        self.assertEqual(ib.rvol(bars, len(bars) - 1)[1], "previous 20 bars")
        self.assertIsNone(ib.slot_rvol([{"date": "2026-10-01", "time": "", "v": 5}], 0), "daily bars have no slot")

    def test_gate3_no_longer_passes_every_overnight_story(self):
        cfg = {"min_wait_minutes": 5, "max_wait_minutes": 15, "volume_ratio": 2.0, "bars": 20}
        start = datetime.datetime.combine(datetime.date.fromisoformat(sessions(8)[-1]["date"]), datetime.time(9, 15))
        now = start + datetime.timedelta(minutes=20)
        ordinary = sessions(8, last_move=0.5)                         # a normal open that drifts up
        self.assertEqual(macro.gate3(ordinary, start, now, "long", cfg)["status"], "dropped")
        heavy = sessions(8, last_open=15000.0, last_move=0.5)         # 3x the usual open, the right way
        g = macro.gate3(heavy, start, now, "long", cfg)
        self.assertEqual(g["status"], "confirmed")
        self.assertEqual(g["volume_vs"], "same slot, last 20 sessions")


class Regime(unittest.TestCase):
    def test_scores(self):
        self.assertEqual(regime.score_volatility({"india_vix": {"level": 25, "range": {"percentile": 90},
                                                                "vs_1w_ago": {"change_pct": 5}}})["score"], -1)
        self.assertEqual(regime.score_volatility({"india_vix": {"level": 11, "range": {"percentile": 20},
                                                                "vs_1w_ago": {"change_pct": -3}}})["score"], 1)
        self.assertEqual(regime.score_breadth({"stock_breadth": {"up_pct": 65}})["score"], 1)
        up = [100 + i * 0.5 for i in range(60)]
        self.assertEqual(regime.score_trend(up)["score"], 1)
        self.assertEqual(regime.score_trend(list(reversed(up)))["score"], -1)
        self.assertIsNone(regime.score_trend(up[:30]))
        self.assertEqual(regime.score_flows([{"category": "FII/FPI", "netValue": "-3500"}])["score"], -1)
        r = regime.regime([{"name": "a", "score": -1}, {"name": "b", "score": -1}, {"name": "c", "score": 0}])
        self.assertEqual((r["regime"], r["size_hint"]), ("risk-off", "half"))

    def test_participant_oi(self):
        with open(os.path.join(REGIME_FIX, "fao_participant_oi_23112026.csv")) as fh:
            poi = regime.parse_participant_oi(fh.read())
        self.assertEqual((poi["FII"]["Future Index Long"], poi["FII"]["Future Index Short"]), (30649, 121598))
        self.assertEqual(regime.score_positioning(poi)["score"], -1)        # 20 % long: FII net short

    def test_offline_run_on_recorded_nse_answers(self):
        code, out, _ = run(regime, ["--offline", REGIME_FIX, "--now", "2026-11-24 08:45", "--json", "--dry-run"])
        doc = json.loads(out)
        got = {c["name"]: c["score"] for c in doc["components"]}
        self.assertEqual(got, {"volatility": -1, "breadth": -1, "trend": 1, "positioning": -1, "flows": 1})
        self.assertEqual((doc["regime"], doc["score"], doc["volatility"], doc["size_hint"]),
                         ("neutral", -1, "high", "half"))
        self.assertIn("pp on the day", [c for c in doc["components"] if c["name"] == "positioning"][0]["value"])
        self.assertEqual(code, 0)

    def test_brief_warns_on_a_long_in_risk_off(self):
        reg = {"regime": "risk-off", "advice": "fewer longs", "volatility": "high"}
        flags = [f["flag"] for f in brief.regime_flags(reg, "long")]
        self.assertEqual(flags, ["regime risk-off", "high volatility"])
        self.assertEqual([f["flag"] for f in brief.regime_flags(reg, "short")], ["high volatility"])
        self.assertEqual(brief.regime_flags(None, "long"), [])


class Backfill(unittest.TestCase):
    def test_filings_first_and_the_deals_breaker(self):
        tmp = tempfile.mkdtemp()
        try:
            fx = os.path.join(tmp, "fx")
            os.makedirs(fx)
            days = [datetime.date(2026, 10, 5) + datetime.timedelta(days=k) for k in range(10)]
            days = [d for d in days if d.weekday() < 5]
            for d in days:
                with open(os.path.join(fx, "bhav_%s.csv" % d.strftime("%d%m%Y")), "w") as fh:
                    fh.write(bhav(d.isoformat(), [("ABC", "EQ", 100, 101, 103, 99, 102, 250)]))
            with open(os.path.join(fx, "ann_2026-10-05.json"), "w") as fh:
                json.dump([], fh)
            with open(os.path.join(fx, "ann_2026-10-12.json"), "w") as fh:
                json.dump([], fh)
            con = ev.connect(os.path.join(tmp, "e.sqlite"))
            client = ev.ih.Client(offline=fx)          # no deals files: every deals request fails
            logs = []
            ev.backfill(con, client, days[0], days[-1], sources=("bars", "filings", "deals"), log=logs.append,
                        today=datetime.date(2026, 11, 23))
            urls = client.calls
            first_ann = min(i for i, u in enumerate(urls) if "corporate-announcements" in u)
            first_bhav = min(i for i, u in enumerate(urls) if "bhavdata" in u)
            self.assertLess(first_ann, first_bhav, "filings load before the daily files")
            self.assertEqual(sum(1 for u in urls if "deals" in u), ev.DEALS_BREAKER)
            self.assertTrue(any("skipped for the rest of this run" in x for x in logs))
            self.assertEqual(con.execute("SELECT count(*) FROM loaded WHERE kind='bars' AND rows>0").fetchone()[0],
                             len(days), "prices still load")
            con.close()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
