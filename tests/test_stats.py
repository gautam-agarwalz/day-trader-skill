"""Step 4 of the audit roadmap: the statistics rebuild. A fixed rule set, the Reality Check against best-of-K
luck, date-block bootstraps, the 30-trade out-of-sample rule, the intraday block model (similar sessions,
volatility scaling, drift removed) and conditioned swing windows. Offline, synthetic data."""

import datetime
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load
from test_events import build_db
import engine_fixtures as fx

ev = load("trader-timemachine", "events")
tm = load("trader-timemachine", "timemachine")

IST = datetime.timedelta(hours=5, minutes=30)


def chart(sessions=30, drift=0.001, today_vol=1.0, seed=4):
    """A Yahoo chart: `sessions` full days that each rise `drift` a bar, then a morning to 10:00."""
    rnd = random.Random(seed)
    stamps, o_, h_, l_, c_, v_ = [], [], [], [], [], []
    d = datetime.date(2026, 8, 3)
    made, p = 0, 100.0
    while made <= sessions:
        if d.weekday() < 5:
            last = made == sessions
            for k in range(75 if not last else 9):
                m = 9 * 60 + 15 + 5 * k
                t = datetime.datetime.combine(d, datetime.time(m // 60, m % 60)) - IST
                o = p
                c = p * (1 + (rnd.gauss(0, 0.004 * today_vol) if last else drift + rnd.gauss(0, 0.001)))
                stamps.append(int((t - datetime.datetime(1970, 1, 1)).total_seconds()))
                o_.append(o), c_.append(c), h_.append(max(o, c) * 1.0005), l_.append(min(o, c) * 0.9995), v_.append(1000)
                p = c
            made += 1
        d += datetime.timedelta(days=1)
    day = d - datetime.timedelta(days=1)
    while day.weekday() >= 5:
        day -= datetime.timedelta(days=1)
    doc = {"chart": {"result": [{"meta": {"symbol": "ABC.NS", "dataGranularity": "5m"}, "timestamp": stamps,
                                 "indicators": {"quote": [{"open": o_, "high": h_, "low": l_, "close": c_,
                                                           "volume": v_}]}}]}}
    return json.dumps(doc), day.isoformat(), c_[-1]


class RealityCheck(unittest.TestCase):
    def rules(self, effect=0.0, k=8, dates=60, per=3, seed=1):
        rnd = random.Random(seed)
        out = {}
        for j in range(k):
            out[j] = [("d%03d" % d, rnd.gauss(effect if j == 0 else 0.0, 1.0)) for d in range(dates) for _ in range(per)]
        return out

    def test_noise_is_called_chance(self):
        ps = [ev.reality_check(self.rules(seed=s), sims=500)["p"] for s in range(5)]
        self.assertGreater(sum(1 for p in ps if p > ev.RC_P), 3, ps)

    def test_a_real_effect_survives(self):
        rc = ev.reality_check(self.rules(effect=0.6), sims=500)
        self.assertLess(rc["p"], 0.02)
        self.assertEqual((rc["rules"], rc["blocks"]), (8, 60))

    def test_date_blocks_widen_the_range_when_trades_share_a_date(self):
        rnd = random.Random(3)
        rets, groups = [], []
        for d in range(40):                   # 5 trades a date that all move together
            x = rnd.gauss(0.2, 2.0)
            rets += [x] * 5
            groups += [d] * 5
        a = ev.bootstrap(rets, 3000)
        b = ev.bootstrap(rets, 3000, groups=groups)
        self.assertEqual(b["blocks"], 40)
        self.assertGreater(b["mean_p95"] - b["mean_p5"], 1.8 * (a["mean_p95"] - a["mean_p5"]))


class Rules(unittest.TestCase):
    def test_the_fixed_set_is_small(self):
        self.assertEqual(len(ev.rule_set(("close0",))), 6)
        self.assertEqual(len(ev.rule_set(("close0", "open1"))), 11)
        self.assertGreater(len(ev.rule_grid(("close0", "open1"))), 150)

    def test_thirty_recent_trades_to_call_it_an_edge(self):
        tmp = tempfile.mkdtemp()
        try:
            con, _ = build_db(os.path.join(tmp, "e.sqlite"), n_events=70, every=9)   # ~27 in the last 12 months
            out, _ = ev.study(con, "order_win", sims=300)
            best = out["rules"]["best"]
            self.assertEqual(best["verdict"], "edge, not checked out of sample")
            self.assertLessEqual(best["selection"]["rules_tested"], 11)
            self.assertLess(best["selection"]["reality_check"]["p"], 0.05)
            con.close()
            con, _ = build_db(os.path.join(tmp, "f.sqlite"), n_events=110, every=6)  # 40+ recent trades
            best = ev.study(con, "order_win", sims=300)[0]["rules"]["best"]
            self.assertEqual(best["verdict"], "edge")
            self.assertGreaterEqual(best["out_of_sample"]["n"], ev.OOS_MIN)
            con.close()
        finally:
            shutil.rmtree(tmp)


class BlockModel(unittest.TestCase):
    def plan(self, day, entry):
        return tm.normalize_plan({"symbol": "ABC", "side": "buy", "date": day, "entry": round(entry, 2),
                                  "stop": round(entry * 0.995, 2), "target": round(entry * 1.005, 2),
                                  "entry_time": "10:00"})

    def run_both(self, **kw):
        text, day, last = chart(**kw)
        now = datetime.datetime.fromisoformat(day + " 04:31")
        bars = tm.time_machine(text, self.plan(day, last), now, replays=2000, model="bars")["mc"]
        blocks = tm.time_machine(text, self.plan(day, last), now, replays=2000, model="blocks")["mc"]
        return bars, blocks

    def test_the_drift_is_taken_out(self):
        bars, blocks = self.run_both(drift=0.0008)
        self.assertGreater(bars["avg_R"], 0.5, "every past day rose: the bar model calls any long a winner")
        self.assertLess(abs(blocks["avg_R"]), 0.35, "the block model removes the drift")
        self.assertGreater(blocks["drift_removed_bp_per_bar"], 5)
        self.assertEqual(blocks["model"], "blocks")
        self.assertEqual(blocks["similar_sessions"], 15)

    def test_a_volatile_morning_scales_the_replays(self):
        _, blocks = self.run_both(drift=0.0, today_vol=4.0)
        self.assertEqual(blocks["vol_scale_median"], tm.SCALE_CLIP[1])


class SwingWindows(unittest.TestCase):
    def test_conditioned_on_days_like_the_last(self):
        w, f = ev.windows_like(fx.daily_bars(), 3)
        self.assertTrue(f["conditioned"])
        self.assertEqual(len(w), max(60, f["of"] // 2))
        self.assertEqual(len(w[0]), 3)
        short, f2 = ev.windows_like(fx.daily_bars(n=40), 3)
        self.assertFalse(f2["conditioned"])


if __name__ == "__main__":
    unittest.main()
