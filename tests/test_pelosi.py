"""trader-pelosi: the Indian smart money & institutional tracker.

Fixtures in tests/fixtures/trader-pelosi/ are real answers recorded from NSE on 2026-11-23
(trimmed: pledge data to 80 companies, the shareholding master to 150 rows, the delivery file to
the day's deal symbols). The AMFI workbooks are SYNTHETIC, built in the test in the SEBI format:
real fund-house files live on each AMC's own site.
"""

import copy
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

sm = load("trader-pelosi", "smartmoney")
ih = sm.ih
DIR = os.path.join(FIX, "trader-pelosi")
NOW = "2026-11-23 21:30"


def fixture(name):
    path = os.path.join(DIR, name)
    if os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Deals(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(fixture("nse_largedeal.json"))
        text = fixture("sec_bhavdata_full_23112026.csv")
        c = ih.Client(offline=DIR)
        self.deliv = sm.delivery_map(c, __import__("datetime").date(2026, 11, 23))
        self.assertIn("LENSKART", self.deliv)
        self.assertTrue(text.startswith("SYMBOL"))

    def test_round_trips_are_intraday_noise(self):
        items, counts = sm.classify_deals(self.doc, self.deliv)
        self.assertEqual(counts["deals"], 94)
        self.assertEqual(counts["round_trip"], 72)          # bought and sold the same stock the same day
        self.assertEqual(counts["kept"], 16)
        self.assertFalse(any("MICROCURVES" in i["title"] for i in items))

    def test_block_deals_and_institutions_are_highlighted(self):
        items, _ = sm.classify_deals(self.doc, self.deliv)
        sg = [i for i in items if i["title"].startswith("SOCIETE GENERALE - ODI bought")][0]
        self.assertTrue(sg["highlight"])
        self.assertEqual((sg["symbol"], sg["value_cr"]), ("LENSKART", 447.79))
        self.assertIn("institution", sg["flags"])
        self.assertIn("delivery 58.4% of the day's volume", sg["detail"])
        self.assertEqual(sum(i["highlight"] for i in items), 7)

    def test_a_trading_desk_on_a_low_delivery_day_is_dropped(self):
        doc = {"BULK_DEALS_DATA": [{"symbol": "XYZ", "clientName": "ACME STOCK BROKING PVT LTD", "buySell": "BUY",
                                    "qty": "500000", "watp": "100", "date": "23-Nov-2026"}]}
        self.assertEqual(sm.classify_deals(doc, {"XYZ": 12.0})[1]["desk_low_delivery"], 1)
        items, _ = sm.classify_deals(doc, {"XYZ": 75.0})
        self.assertEqual(len(items), 1)
        self.assertIn("high delivery", items[0]["flags"])
        self.assertTrue(items[0]["highlight"])                 # Rs 5 cr at 75 % delivery


class Insider(unittest.TestCase):
    def test_the_xbrl_is_read_per_person(self):
        recs = sm.parse_insider_xbrl(fixture("IT_1197873_1662973_02052026044642_WEB.xml"))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["symbol"], r["person"], r["category"], r["type"], r["mode"]),
                         ("RELTD", "Shantanu Lath", "Director", "Buy", "ESOP"))
        self.assertEqual((r["qty"], r["before_pct"], r["after_pct"]), ("70000", 0.03, 0.07))
        it = sm.insider_item(r, "x", "y")
        self.assertFalse(it["highlight"])                     # an ESOP allotment, not an open-market buy

    def test_promoter_open_market_buys_are_highlighted(self):
        rec = {"symbol": "ABC", "person": "P", "category": "Promoter Group", "type": "Buy",
               "mode": "Market Purchase", "qty": "1000", "value": "5000000", "date": "2026-11-20"}
        self.assertTrue(sm.insider_item(rec, "x", "y")["highlight"])
        self.assertFalse(sm.insider_item(dict(rec, type="Sell"), "x", "y")["highlight"])
        self.assertFalse(sm.insider_item(dict(rec, category="Employees/Designated Employees"), "x", "y")["highlight"])

    def test_html_escapes_are_undone(self):
        names = []
        for f in os.listdir(DIR):
            if f.startswith("IT_"):
                names += [r["person"] for r in sm.parse_insider_xbrl(fixture(f))]
        self.assertIn("Bajaj Holdings & Investment Limited", names)


class SastPledgeShareholding(unittest.TestCase):
    def test_sast_highlights_market_moves_only(self):
        items = sm.sast_items(json.loads(fixture("nse_sast.json")))
        self.assertEqual(len(items), 121)
        self.assertEqual(sum(i["highlight"] for i in items), 27)
        swiggy = [i for i in items if i["symbol"] == "SWIGGY"][0]
        self.assertIn("crossed 5%", swiggy["flags"])             # 4.99 % -> 5.17 %
        self.assertTrue(swiggy["highlight"])
        self.assertTrue(all("open market" in i["flags"] for i in items if i["highlight"]))

    def test_pledge_changes_against_the_last_run(self):
        cur = sm.pledge_snapshot(json.loads(fixture("nse_pledge.json")))
        self.assertEqual(sm.pledge_changes(None, cur), [])     # the first run is the baseline
        prev = copy.deepcopy(cur)
        name = sorted(n for n, v in cur.items() if v["num"] > 0)[0]
        prev[name]["num"] = cur[name]["num"] * 2               # it had twice as many pledged last time
        fresh = sorted(n for n, v in cur.items() if v["num"] > 0)[1]
        prev[fresh]["num"] = 0.0                               # none pledged last time
        gone = dict(cur[name], num=0.0, pct=0.0)
        got = {i["company"]: i["title"] for i in sm.pledge_changes(prev, dict(cur, **{name: gone}))}
        self.assertEqual(got[name], "%s: pledge fully revoked" % name)
        self.assertEqual(got[fresh], "%s: fresh pledge" % fresh)
        got = {i["company"]: i["title"] for i in sm.pledge_changes(prev, cur)}
        self.assertEqual(got[name], "%s: pledge reduced (released)" % name)
        self.assertEqual(len(got), 2)

    def test_shareholding_xbrl_split(self):
        cur = sm.parse_shp_xbrl(fixture("SHP_1760080_19112026032252_WEB.xml"))
        self.assertEqual((cur["symbol"], cur["date"]), ("VEEFIN", "2026-11-09"))
        self.assertEqual((cur["promoter"], cur["fii"], cur["dii"], cur["public"]), (34.73, 2.22, 0.0, 65.27))
        it = sm.shp_item(cur, dict(cur, fii=0.5, date="2026-09-30"), "link")
        self.assertIn("FII +1.72 pp", it["detail"])
        self.assertEqual(it["flags"], ["FII up"])
        self.assertTrue(it["highlight"])

    def test_promoter_shift_needs_a_newer_quarter(self):
        cur = sm.master_snapshot(json.loads(fixture("nse_shp_master.json")))
        self.assertEqual(sm.promoter_shifts(cur, cur), [])
        name = sorted(cur)[0]
        prev = {name: dict(cur[name], promoter=cur[name]["promoter"] + 2.5, when="2026-06-30 00:00", date="30-JUN-2026")}
        got = sm.promoter_shifts(prev, cur)
        self.assertEqual(len(got), 1)
        self.assertIn("promoter stake down 2.50 pp", got[0]["title"])


class FiiDii(unittest.TestCase):
    def test_the_days_flows(self):
        items = sm.fiidii_items(json.loads(fixture("nse_fiidii.json")))
        dii = [i for i in items if i["symbol"] == "DII"][0]
        self.assertEqual(dii["title"], "DII net sold Rs 2,760.12 cr on 23-Nov-2026")
        self.assertTrue(dii["highlight"])                      # over Rs 1,000 cr
        self.assertEqual(dii["value_cr"], -2760.12)


def workbook(path, rows):
    """A SYNTHETIC fund-house portfolio file in the SEBI layout: a title block, then a header row."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Flexi Cap Fund"
    ws.append(["XYZ Mutual Fund"])
    ws.append(["Monthly Portfolio Statement as on October 31, 2026"])
    ws.append([])
    ws.append(["Name of the Instrument", "ISIN", "Industry", "Quantity", "Market value (Rs. in Lakhs)", "% to Net Assets"])
    for r in rows:
        ws.append(r)
    ws.append(["Government Bond 2030", "IN0020200001", "Sovereign", 1000, 10, 0.1])   # not equity: ignored
    wb.save(path)


class Amfi(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_the_directory_lists_fund_house_pages(self):
        links = sm.amfi_directory('x "https://amc.ppfas.com/downloads/portfolio-disclosure/\\" '
                                  '"https://choicemf.com/disclosures/monthly-portfolio\\" "https://www.amfiindia.com/x"')
        self.assertEqual([l["amc_site"] for l in links], ["amc.ppfas.com", "choicemf.com"])

    def test_fresh_buys_and_stake_increases(self):
        oct_, nov = os.path.join(self.d, "oct.xlsx"), os.path.join(self.d, "nov.xlsx")
        workbook(oct_, [["Reliance Industries Ltd", "INE002A01018", "Petroleum", 100000, 1300, 4.1],
                        ["Tata Consultancy Services Ltd", "INE467B01029", "IT", 50000, 1200, 3.8]])
        workbook(nov, [["Reliance Industries Ltd", "INE002A01018", "Petroleum", 130000, 1700, 5.0],
                       ["Tata Consultancy Services Ltd", "INE467B01029", "IT", 50000, 1200, 3.8],
                       ["Infosys Ltd", "INE009A01021", "IT", 20000, 300, 1.0]])
        self.assertEqual(set(sm.parse_portfolio_excel(oct_)), {"INE002A01018", "INE467B01029"})
        st = os.path.join(self.d, "state.json")
        base = ["amfi", "--amc", "XYZ MF", "--state", st, "--out", os.path.join(self.d, "out"), "--json"]
        code, text, _ = run(base + ["--file", oct_, "--month", "2026-10"])
        self.assertEqual(json.loads(text)["items"], [])            # the first month is the baseline
        code, text, _ = run(base + ["--file", nov, "--month", "2026-11"])
        titles = sorted(i["title"] for i in json.loads(text)["items"])
        self.assertEqual(titles, ["XYZ MF: fresh buy in Infosys Ltd", "XYZ MF: stake increased 30% in Reliance Industries Ltd"])
        code, text, _ = run(base + ["--file", nov, "--month", "2026-11"])
        self.assertEqual([i["new"] for i in json.loads(text)["items"]], [False, False])   # never flagged twice


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--offline", DIR, "--now", NOW, "--state", os.path.join(self.d, "state.json"),
                     "--out", os.path.join(self.d, "out"), "--amfi-config", os.path.join(self.d, "none.json")]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_a_run_saves_json_and_csv_and_dedupes(self):
        code, text, _ = run(self.base)
        self.assertEqual(code, 0)
        for heading in ("== Bulk/Block deal (16, 7 highlighted)", "== SAST / Takeover (121, 27 highlighted)",
                        "== FII / DII flows (2, 2 highlighted)", "72 round trips"):
            self.assertIn(heading, text)
        self.assertIn("Not financial advice", text)
        with open(os.path.join(self.d, "out", "latest.json"), encoding="utf-8") as fh:
            latest = json.load(fh)
        with open(os.path.join(self.d, "out", "alerts.csv"), encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        self.assertEqual(rows[0], ",".join(sm.CSV_FIELDS))
        self.assertEqual(len(rows) - 1, sum(1 for i in latest["items"] if i["new"]))
        code, text, _ = run(self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", text)
        with open(os.path.join(self.d, "out", "alerts.csv"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), len(rows))   # nothing appended twice

    def test_dry_run_writes_nothing(self):
        code, text, _ = run(self.base + ["--dry-run", "--only", "deals,fiidii"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, "out")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "state.json")))

    def test_a_missing_source_is_a_note_not_a_crash(self):
        empty = os.path.join(self.d, "empty")
        os.mkdir(empty)
        code, text, _ = run(["--offline", empty, "--now", NOW, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("note: deals: no offline file", text)
        self.assertNotIn("Traceback", text)
        code, _, err = run(self.base + ["--only", "moon"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
