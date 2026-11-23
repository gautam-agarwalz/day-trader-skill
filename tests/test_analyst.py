"""trader-analyst: a company's quarters from NSE's integrated results filings.

Fixtures in tests/fixtures/trader-analyst/ are real answers recorded from NSE on 2026-11-23:
TCS (consolidated, Ind AS) and HDFC Bank (the banking taxonomy), their filing lists, each
quarter's XBRL (gzipped) and their board meetings.
"""

import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("trader-analyst", "facts")
ir = facts.ir
DIR = os.path.join(FIX, "trader-analyst")
NOW = "2026-11-23 21:30"


def xbrl(name):
    with gzip.open(os.path.join(DIR, name + ".gz"), "rt", encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


class Xbrl(unittest.TestCase):
    def test_a_quarter_and_the_year_to_date(self):
        x = ir.parse_results_xbrl(xbrl("INTEGRATED_FILING_INDAS_1734607_08102026100810_WEB.xml"))
        self.assertEqual((x["symbol"], x["nature"]), ("TCS", "Consolidated"))
        self.assertEqual((str(x["period_start"]), str(x["period_end"])), ("2026-07-01", "2026-09-30"))
        q = x["quarter"]
        self.assertEqual((q["revenue"], q["profit"], q["pbt"], q["eps"]), (731880000000.0, 139340000000.0, 186380000000.0, 38.37))
        self.assertEqual(x["ytd"]["revenue"], 1454630000000.0)            # April to September

    def test_a_bank_files_interest_earned(self):
        x = ir.parse_results_xbrl(xbrl("INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml"))
        self.assertEqual(x["symbol"], "HDFCBANK")
        self.assertAlmostEqual(x["quarter"]["revenue"] / 1e7, 95132, places=0)
        self.assertIsNotNone(x["quarter"]["profit"])


class Quarters(unittest.TestCase):
    def test_tcs(self):
        code, text, _ = run(["TCS", "--offline", DIR, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        r = json.loads(text)["TCS"]
        q = r["quarters"][0]
        self.assertEqual((q["period_end"], q["revenue_cr"], q["profit_cr"], q["eps"]), ("2026-09-30", 73188.0, 13934.0, 38.37))
        self.assertEqual((q["revenue_yoy"], q["profit_yoy"], q["net_margin"]), (11.2, 14.9, 19.0))
        self.assertEqual(r["ttm"], {"revenue_cr": 283248.0, "profit_cr": 51858.0, "eps": 142.64})
        self.assertEqual(r["latest_filing"]["broadcast"], "2026-10-08 22:07")
        self.assertIn("Revenue +11.2% on the same quarter last year and +1.3% on the quarter before.", r["changes"])

    def test_the_text_and_metrics(self):
        d = tempfile.mkdtemp()
        try:
            m = os.path.join(d, "metrics.json")
            code, text, _ = run(["TCS", "HDFCBANK", "--offline", DIR, "--now", NOW, "--metrics", m])
            self.assertEqual(code, 0)
            self.assertIn("TCS (Tata Consultancy Services Limited), consolidated quarters (Rs crore, from NSE filings)", text)
            self.assertIn("Not financial advice", text)
            with open(m) as fh:
                rows = json.load(fh)
            self.assertEqual(sorted(rows), ["HDFCBANK", "TCS"])
            self.assertEqual(rows["TCS"]["revenue_cr"], 73188.0)
        finally:
            shutil.rmtree(d)

    def test_an_unknown_symbol(self):
        empty = tempfile.mkdtemp()
        try:
            code, _, err = run(["NOPE", "--offline", empty, "--now", NOW])
            self.assertEqual(code, 2)
            self.assertIn("NOPE", err)
        finally:
            shutil.rmtree(empty)

    def test_next_results_meeting(self):
        meetings = [{"symbol": "X", "date": "2026-12-05", "purpose": "Financial Results", "desc": ""},
                    {"symbol": "X", "date": "2026-11-30", "purpose": "Fund Raising", "desc": "to consider fund raising"},
                    {"symbol": "X", "date": "2026-11-01", "purpose": "Financial Results", "desc": ""}]
        self.assertEqual(ir.next_results_meeting(meetings, "2026-11-23")["date"], "2026-12-05")


if __name__ == "__main__":
    unittest.main()
