"""Swing mode across the chain: an overnight event idea -> exit plan -> budget -> risk -> alerts.

A swing plan holds past the close, so it has an exit_by date instead of a flat-by time, is sized on
the larger of the stop and the overnight gap risk, and is blocked unless the config turns overnight
holds on ("swing": {"allow_overnight": true})."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

exitp = load("trader-exit", "exit")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
ev = load("trader-timemachine", "events")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
DAY = "2026-11-23"            # a Monday; the 24th is an NSE holiday (Guru Nanak Jayanti)


def cfg(**swing):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c["max_loss_per_trade_inr"] = 500
    c["max_daily_loss_inr"] = 1500
    c["max_order_inr"] = 20000
    c["budget"] = 50000
    c["swing"].update({"max_loss_per_trade_inr": 500, "max_overnight_inr": 20000})
    c["swing"].update(swing)
    return c


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


IDEA = {"symbol": "LENSKART", "type": "order_win", "event_time": DAY + " 13:05", "link": "https://x/a.pdf",
        "rule": "buy at the day-0 close (15:10-15:25), sell at the next open",
        "rule_spec": {"entry": "close0", "kind": "gap", "days": 1, "cost": 0.3},
        "entry": "buy 15:10-15:25 on %s, just before the close" % DAY, "exit": "the next open",
        "entry_day": DAY, "exit_by": "2026-11-25", "reference_price": 725.0, "stop": None, "target": None,
        "atr_pct": 2.5, "worst_5pct": -3.2, "trades": 120, "average_net_pct": 0.8, "win_pct": 61.0,
        "verdict": "edge"}


class ExitSwing(unittest.TestCase):
    def test_swing_plan_has_an_exit_by_and_no_flat_by(self):
        code, out, err = run(exitp, ["plan", "--symbol", "LENSKART", "--entry", "725", "--setup", "level",
                                     "--stop", "pct:3", "--target", "1.5R", "--date", DAY, "--mode", "swing",
                                     "--hold", "1", "--gap-risk", "3.2", "--now", DAY + " 15:10", "--json"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan["mode"], plan["flat_by"], plan["hold_sessions"]), ("swing", None, 1))
        self.assertEqual(plan["exit_by"], "2026-11-25", "the 24th is a holiday, so one session ahead is the 25th")
        self.assertEqual(plan["gap_risk_pct"], 3.2)
        self.assertTrue(any("Swing: held overnight" in w for w in plan["warnings"]))

    def test_swing_flags_are_refused_in_day_mode_and_bad_dates(self):
        base = ["plan", "--symbol", "X", "--entry", "100", "--setup", "level", "--stop", "pct:1", "--date", DAY,
                "--now", DAY + " 10:00"]
        code, _, err = run(exitp, base + ["--hold", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--mode swing", err)
        code, _, err = run(exitp, base + ["--mode", "swing", "--exit-by", DAY])
        self.assertEqual(code, 1)
        self.assertIn("not after the entry day", err)
        code, _, err = run(exitp, base + ["--mode", "swing", "--exit-by", "2026-11-24"])
        self.assertIn("not an NSE session", err)

    def test_describe_says_exit_by(self):
        code, out, _ = run(exitp, ["plan", "--symbol", "X", "--entry", "100", "--setup", "level", "--stop", "pct:1",
                                   "--date", DAY, "--mode", "swing", "--hold", "3", "--now", DAY + " 15:00"])
        self.assertEqual(code, 0)
        self.assertIn("Exit by       the close of 2026-11-27 (swing, 3 sessions held overnight)", out)
        self.assertNotIn("no overnight hold", out)


class BudgetSwing(unittest.TestCase):
    def plan(self, **over):
        p = ev.plan_from_idea(IDEA, 1.0)
        p.update(over)
        return p

    def test_the_gap_risk_sizes_it_when_it_is_bigger_than_the_stop(self):
        p = self.plan(stop=715.0, target=740.0)      # a 10-rupee stop under a 3.2 % gap risk
        s = size.size_trade(p, cfg(), today=DAY)
        risk, gap = s["risk_per_share"], 725 * 0.032
        self.assertLess(risk, gap)
        self.assertEqual(s["bound_by"], "gap risk")
        self.assertEqual(s["qty"], int(500 // gap))
        o = size.bracket_order(s)
        self.assertEqual(o["plan"]["mode"], "swing")
        self.assertEqual(o["plan"]["exit_by"], "2026-11-25")
        self.assertIn("delivery", o["product"])

    def test_the_overnight_cap_binds(self):
        s = size.size_trade(self.plan(), cfg(max_overnight_inr=3000), today=DAY)
        self.assertEqual((s["bound_by"], s["qty"]), ("overnight cap", 4))

    def test_a_day_plan_ignores_the_gap(self):
        p = self.plan(mode="day", gap_risk_pct=9.0, flat_by="15:15")
        s = size.size_trade(p, cfg(), today=DAY)
        self.assertNotEqual(s["bound_by"], "gap risk")
        self.assertEqual(s["flat_by"], "15:15")


class RiskSwing(unittest.TestCase):
    def order(self, plan=None, **swing):
        s = size.size_trade(plan or ev.plan_from_idea(IDEA, 1.0), cfg(**swing), today=DAY)
        return size.bracket_order(s)

    def verdict(self, order, config, now):
        res = check.check([order], config, now=check.parse_now(now))
        return res["results"][0]

    def test_blocked_until_overnight_holds_are_turned_on(self):
        r = self.verdict(self.order(), cfg(), DAY + " 15:15")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("overnight holds are off" in x for x in r["reasons"]), r["reasons"])

    def test_passes_after_the_day_trade_window_when_on(self):
        on = cfg(allow_overnight=True)
        r = self.verdict(self.order(allow_overnight=True), on, DAY + " 15:15")
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any("swing: held overnight to the close of 2026-11-25" in x for x in r["info"]))
        late = self.verdict(self.order(allow_overnight=True), on, DAY + " 15:27")
        self.assertTrue(any("no new entries after 15:25" in x for x in late["reasons"]), late["reasons"])

    def test_the_same_order_as_a_day_trade_is_blocked_after_14_50(self):
        o = self.order(allow_overnight=True)
        o["plan"].pop("mode")
        r = self.verdict(o, cfg(allow_overnight=True), DAY + " 15:15")
        self.assertTrue(any("no new entries after 14:50" in x for x in r["reasons"]), r["reasons"])

    def test_hold_length_and_gap_loss(self):
        o = self.order(allow_overnight=True)
        o["plan"]["exit_by"] = "2026-12-31"
        r = self.verdict(o, cfg(allow_overnight=True), DAY + " 15:15")
        self.assertTrue(any("max_hold_days" in x for x in r["reasons"]), r["reasons"])
        tight = dict(ev.plan_from_idea(IDEA, 1.0), stop=715.0, target=740.0)
        o = self.order(tight, allow_overnight=True)
        o["qty"] = "40"                                   # 40 x 10 = 400 at the stop, but 40 x 23.20 on the gap
        o["stop_loss_stop_price"] = "715.00"
        r = self.verdict(o, cfg(allow_overnight=True, max_overnight_inr=50000), DAY + " 15:15")
        self.assertTrue(any(x.startswith("gap: ") for x in r["reasons"]), r["reasons"])
        self.assertFalse([x for x in r["reasons"] if x.startswith("loss:")], r["reasons"])

    def test_bare_nse_symbols_are_accepted(self):
        r = self.verdict(self.order(allow_overnight=True), cfg(allow_overnight=True), DAY + " 15:15")
        self.assertFalse([x for x in r["reasons"] if x.startswith("asset")])


class AlertsSwing(unittest.TestCase):
    def test_rules_last_until_the_exit_day(self):
        plan = ev.plan_from_idea(IDEA, 1.0)
        rules = {r["id"]: r for r in alerts.rules_from_plan(plan)}
        self.assertNotIn("lenskart-flat-by", rules)
        self.assertEqual((rules["lenskart-plan-stop"]["date"], rules["lenskart-plan-stop"]["until"]),
                         (DAY, "2026-11-25"))
        gap = rules["lenskart-gap-check"]
        self.assertEqual((gap["date"], gap["until"], gap["at"]), ("2026-11-24", "2026-11-25", "09:20"))
        self.assertEqual(rules["lenskart-exit-by"]["date"], "2026-11-25")
        self.assertTrue(alerts.active(rules["lenskart-plan-stop"], "2026-11-25"))
        self.assertFalse(alerts.active(rules["lenskart-plan-stop"], "2026-11-26"))

    def test_the_gap_check_rings_the_next_morning(self):
        plan = ev.plan_from_idea(IDEA, 1.0)
        rules = [r for r in alerts.rules_from_plan(plan) if r["id"] == "lenskart-gap-check"]
        now = alerts.parse_now("2026-11-25 09:21")
        status = alerts.market_status(now)
        rings, _ = alerts.evaluate(rules, {}, status, {}, now)
        self.assertEqual(len(rings), 1)
        self.assertIn("re-place the stop", rings[0]["message"])
        self.assertNotIn("flatten", rings[0]["message"])


class EventPlan(unittest.TestCase):
    def test_plan_from_a_time_exit_idea(self):
        p = ev.plan_from_idea(IDEA, 1.0)
        self.assertEqual((p["mode"], p["exit_by"], p["hold_sessions"], p["date"]), ("swing", "2026-11-25", 1, DAY))
        self.assertAlmostEqual(p["stop"], 701.8, places=2)     # 725 x (1 - 3.2 %)
        self.assertAlmostEqual(p["target"], 748.2, places=2)   # 1R above
        self.assertEqual(p["gap_risk_pct"], 3.2)
        self.assertIn("ceiling", p["target_label"])

    def test_rounding_never_puts_reward_risk_under_the_minimum(self):
        kec = dict(IDEA, symbol="KEC", reference_price=327.75, atr_pct=2.9, worst_5pct=-1.5)
        p = ev.plan_from_idea(kec, 1.0)
        self.assertEqual((p["stop"], p["target"]), (318.24, 337.26))
        self.assertGreaterEqual((p["target"] - p["entry"]) / (p["entry"] - p["stop"]), 1.0)

    def test_cli_plan_writes_trader_plan(self):
        tmp = tempfile.mkdtemp()
        try:
            ideas = os.path.join(tmp, "latest.json")
            with open(ideas, "w") as fh:
                json.dump({"ideas": [IDEA]}, fh)
            out = os.path.join(tmp, "plan.json")
            code, text, err = run(ev, ["--db", os.path.join(tmp, "e.sqlite"), "plan", "1", "--ideas", ideas,
                                       "--config", os.path.join(tmp, "none.json"), "--write", out])
            self.assertEqual(code, 0, err)
            self.assertIn("Exit by the close of 2026-11-25", text)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["symbol"], "LENSKART")
            code, _, err = run(ev, ["--db", os.path.join(tmp, "e.sqlite"), "plan", "NOPE", "--ideas", ideas])
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()


SHORT_IDEA = dict(IDEA, symbol="XYZ", type="auditor_resignation", side="short", instrument="futures", lot_size=500,
                  rule="short (sell first) at the day-0 close (15:10-15:25), cover at the next open",
                  entry="short (sell first) 15:10-15:25 on %s, just before the close" % DAY, reference_price=200.0,
                  worst_5pct=-2.5, atr_pct=2.0)


class Shorts(unittest.TestCase):
    def test_exit_plans_an_intraday_cash_short_and_an_overnight_futures_short(self):
        code, out, err = run(exitp, ["plan", "--symbol", "XYZ", "--entry", "200", "--setup", "level", "--side", "short",
                                     "--stop", "pct:1", "--target", "2R", "--date", DAY, "--now", DAY + " 10:00", "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        self.assertEqual((p["side"], p["stop"], p["target"], p["flat_by"]), ("sell", 202.0, 196.0, "15:15"))
        code, out, err = run(exitp, ["plan", "--symbol", "XYZ", "--entry", "200", "--setup", "level", "--side", "short",
                                     "--stop", "pct:2", "--date", DAY, "--mode", "swing", "--instrument", "futures",
                                     "--lot", "500", "--now", DAY + " 15:10", "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        self.assertEqual((p["instrument"], p["lot_size"], p["mode"]), ("futures", 500, "swing"))
        code, _, err = run(exitp, ["plan", "--symbol", "XYZ", "--entry", "200", "--setup", "level", "--side", "short",
                                   "--stop", "pct:2", "--date", DAY, "--instrument", "futures", "--now", DAY + " 10:00"])
        self.assertIn("needs --lot", err)

    def test_futures_are_sized_in_whole_lots_or_refused(self):
        plan = ev.plan_from_idea(SHORT_IDEA, 1.0)
        c = cfg(max_loss_per_trade_inr=5000, max_overnight_inr=500000)
        c["budget"] = 200000
        s = size.size_trade(plan, c, today=DAY)
        self.assertEqual(s["qty"] % 500, 0)
        self.assertGreater(s["qty"], 0)
        o = size.bracket_order(s)
        self.assertEqual(o["side"], "sell")
        self.assertIn("stock futures", o["product"])
        small = size.size_trade(plan, cfg(), today=DAY)
        self.assertEqual(small["qty"], 0)
        self.assertIn("one lot of 500 XYZ shares", small["refusal"])

    def test_risk_blocks_shorts_until_allowed_and_checks_lots(self):
        plan = ev.plan_from_idea(SHORT_IDEA, 1.0)
        c = cfg(max_loss_per_trade_inr=5000, max_overnight_inr=500000, allow_overnight=True)
        c["budget"] = 200000
        c["max_daily_loss_inr"] = 10000
        order = size.bracket_order(size.size_trade(plan, c, today=DAY))
        r = check.check([order], c, now=check.parse_now(DAY + " 15:15"))["results"][0]
        self.assertTrue(any("short selling is off" in x for x in r["reasons"]), r["reasons"])
        c["allow_short"] = True
        r = check.check([order], c, now=check.parse_now(DAY + " 15:15"))["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["kind"], "entry")
        odd = dict(order, qty="750")
        r = check.check([odd], c, now=check.parse_now(DAY + " 15:15"))["results"][0]
        self.assertTrue(any("whole number of lots" in x for x in r["reasons"]), r["reasons"])
        cash = json.loads(json.dumps(order))
        cash["plan"].pop("instrument")
        r = check.check([cash], c, now=check.parse_now(DAY + " 15:15"))["results"][0]
        self.assertTrue(any("overnight cash short is not possible" in x for x in r["reasons"]), r["reasons"])

    def test_alerts_flip_for_a_short(self):
        plan = ev.plan_from_idea(SHORT_IDEA, 1.0)
        rules = {r["id"]: r for r in alerts.rules_from_plan(plan)}
        self.assertEqual(rules["xyz-plan-stop"]["type"], "above")
        self.assertEqual(rules["xyz-plan-target"]["type"], "below")
        now = alerts.parse_now("2026-11-25 10:00")
        rings, _ = alerts.evaluate([rules["xyz-plan-stop"]], {"XYZ": {"price": plan["stop"] + 1}},
                                   alerts.market_status(now), {}, now)
        self.assertIn("your stop", rings[0]["message"])
