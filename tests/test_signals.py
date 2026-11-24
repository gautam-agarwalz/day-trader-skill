"""india_signals: what trader-news and trader-pelosi saved about a symbol, for the trading tools.

The catalyst and smart-money files here are written by the test in the shape news.py and
smartmoney.py write them (latest.json items and alerts.csv rows)."""

import csv
import datetime
import json
import os
import shutil
import tempfile
import unittest

from _load import load, ROOT

sig = load("trader-scanner", "india_signals")
NOW = datetime.datetime(2026, 11, 24, 10, 0)
TOOLS = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-risk", "trader-alerts"]


class Signals(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.base, "catalysts"))
        os.makedirs(os.path.join(self.base, "smart_money"))
        cats = [
            {"id": "upcoming|ABC|2026-11-25|Financial Results", "source": "NSE event calendar", "tag": "results",
             "tags": ["results"], "symbol": "ABC", "title": "ABC: board meeting on 2026-11-25 to consider financial results",
             "detail": "To consider and approve the financial results", "time": "2026-11-24 00:00",
             "event_date": "2026-11-25", "link": "l1", "numbers": {}, "flags": []},
            {"id": "band|2026-11-24|ABC", "source": "NSE price band changes", "tag": "exchange", "tags": ["exchange"],
             "symbol": "ABC", "title": "ABC: price band 10% -> 5% (tightened)", "detail": "", "time": "2026-11-24 00:00",
             "link": "l2", "numbers": {}, "flags": ["circuit"]},
            {"id": "old", "source": "x", "tag": "orders", "tags": ["orders"], "symbol": "ABC", "title": "ABC: old order",
             "detail": "", "time": "2026-11-01 12:00", "link": "", "numbers": {}, "flags": []},
        ]
        with open(os.path.join(self.base, "catalysts", "latest.json"), "w") as fh:
            json.dump({"items": cats}, fh)
        with open(os.path.join(self.base, "smart_money", "alerts.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["time", "category", "symbol", "company", "title", "detail", "value_cr",
                                               "highlight", "flags", "source", "link", "id"])
            w.writeheader()
            w.writerow({"time": "2026-11-23 18:19", "category": "SAST / Takeover", "symbol": "ABC", "company": "Abc Ltd",
                        "title": "Promoter Co sold 1.5% of ABC (Open Market)", "detail": "", "value_cr": "",
                        "highlight": "True", "flags": "Reg29(2) | sale | promoter | open market", "source": "NSE SAST Reg 29",
                        "link": "l3", "id": "sast|1"})

    def tearDown(self):
        shutil.rmtree(self.base)

    def test_recent_items_and_meetings_ahead(self):
        s = sig.signals_for("abc.ns", NOW, base=self.base)
        self.assertEqual([c["id"] for c in s["catalysts"]], ["upcoming|ABC|2026-11-25|Financial Results", "band|2026-11-24|ABC"])
        self.assertEqual([m["id"] for m in s["smart_money"]], ["sast|1"])
        self.assertEqual(sig.signals_for("XYZ", NOW, base=self.base), {"symbol": "XYZ", "catalysts": [], "smart_money": []})

    def test_flags(self):
        s = sig.signals_for("ABC", NOW, base=self.base)
        flags = [f["flag"] for f in sig.risk_flags(s, NOW)]
        self.assertEqual(flags, ["results due 2026-11-25", "price band 5%", "promoter selling"])
        self.assertIn("results due 2026-11-25", sig.summary(s, sig.risk_flags(s, NOW)))

    def test_describe_and_missing_files(self):
        lines, s, flags = sig.describe("ABC", NOW, base=self.base)
        self.assertTrue(lines[0].startswith("Catalysts for ABC"))
        self.assertIn("  ! promoter selling: Promoter Co sold 1.5% of ABC (Open Market)", lines)
        self.assertEqual(sig.describe("ABC", NOW, base=os.path.join(self.base, "nothing"))[0], [])

    def test_the_copies_are_identical(self):
        with open(os.path.join(ROOT, "trader-scanner", "india_signals.py"), encoding="utf-8") as fh:
            ref = fh.read()
        for t in TOOLS:
            with open(os.path.join(ROOT, t, "india_signals.py"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), ref, t)


if __name__ == "__main__":
    unittest.main()
