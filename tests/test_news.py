"""trader-news: the Indian corporate filings & exchange catalyst scanner.

Fixtures in tests/fixtures/trader-news/ are real answers recorded from NSE, BSE and SEBI on
2026-11-23 (BSE's announcements RSS trimmed to its first 120 items). The pdf-*.pdf files are page
cuts of real filings: two buyback letters (Global Education, Dalmia Bharat), the third page of a
results release (Relaxo) and a results cover letter (Bombay Coated and Special Steels).
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

news = load("trader-news", "news")
ih = news.ih
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-11-23 21:30"


def pdf(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(argv)
    return code, out.getvalue(), err.getvalue()


class Tags(unittest.TestCase):
    def test_nse_categories_first(self):
        self.assertEqual(news.tags_for("", "Bagging/Receiving of orders/contracts"), ["orders"])
        self.assertEqual(news.tags_for("", "Investor Presentation"), ["documents"])
        self.assertEqual(news.tags_for("approved the buyback of equity shares", "Outcome of Board Meeting"), ["buyback"])
        self.assertEqual(news.tags_for("the financial results for the quarter ended September 30, 2026",
                                       "Outcome of Board Meeting"), ["results"])

    def test_text_rules(self):
        self.assertEqual(news.tags_for("Introduction of Futures & Options Contracts on Two Individual Securities"),
                         ["exchange"])
        self.assertEqual(news.tags_for("CHANGES IN PRICE BAND"), ["exchange"])
        self.assertEqual(news.tags_for("received a Letter of Award worth Rs 120 crore"), ["orders"])
        self.assertEqual(news.tags_for("record date for the interim dividend"), ["buyback"])
        self.assertIn("documents", news.tags_for("transcript of the earnings call"))
        self.assertEqual(news.tags_for("change in registered office"), ["other"])

    def test_the_company_name_is_not_a_trigger(self):
        self.assertEqual(news.tags_for("GSM Foils Limited: Resignation of Statutory Auditor", company="GSM Foils Limited"),
                         ["other"])
        self.assertEqual(news.tags_for("Applicability of Graded Surveillance Measure (GSM)"), ["exchange"])

    def test_document_kinds(self):
        it = news.item(title="X: Analysts/Institutional Investor Meet/Con. Call Updates",
                       detail="the transcript of the earnings call", tags=["documents"])
        self.assertIn("transcript", it["flags"])


class PdfNumbers(unittest.TestCase):
    def test_buyback_price_and_size(self):
        n = news.extract_numbers(news.pdf_text(pdf("pdf-buyback-DALBHARAT.pdf")), ["buyback"])
        self.assertEqual(n["buyback_price"], "INR 1,850")
        self.assertEqual(n["buyback_size"], "INR 700,00,00,000")
        n = news.extract_numbers(news.pdf_text(pdf("pdf-buyback-GLOBAL.pdf")), ["buyback"])
        self.assertEqual(n["buyback_price"], "₹162")

    def test_results_from_a_press_release(self):
        n = news.extract_numbers(news.pdf_text(pdf("pdf-results-RELAXO-p3.pdf"), 1), ["results"])
        self.assertEqual(n["revenue"], "INR 702 crores")
        self.assertEqual(n["net_profit"], "INR 37 crores")

    def test_a_cover_letter_has_no_numbers(self):
        text = news.pdf_text(pdf("pdf-results-BCSSL-cover.pdf"))
        self.assertIn("Financial Results", text)
        self.assertEqual({k: v for k, v in news.extract_numbers(text, ["results"]).items() if k != "unit"}, {})

    def test_table_lines(self):
        table = ("Particulars Quarter ended\n(Rs. in lakhs)\nRevenue from operations 12,345.67 11,000.10 9,876.00\n"
                 "Total income 12,500.00 11,100.00 10,000.00\nNet profit for the period 1,234.50 1,100.00 987.00\n"
                 "Basic EPS (Rs.) 4.56 4.10 3.70\n")
        n = news.extract_numbers(table, ["results"])
        self.assertEqual((n["revenue"], n["total_income"], n["net_profit"], n["eps"], n["unit"]),
                         ("12,345.67", "12,500.00", "1,234.50", "4.56", "lakhs"))


class Sources(unittest.TestCase):
    def test_nse_announcements_skip_noise_and_keep_the_pdf(self):
        rows = json.loads(ih.Client(offline=DIR).get_text("x", "nse_announcements.json"))
        items = news.nse_announcement_items(rows)
        self.assertLess(len(items), len(rows))
        self.assertFalse(any("Newspaper" in i["title"] for i in items))
        orders = [i for i in items if i["tag"] == "orders"]
        self.assertTrue(orders)
        self.assertTrue(all(i["link"].startswith("https://nsearchives.nseindia.in/") for i in orders if i["link"]))

    def test_circulars_only_when_they_move_stocks(self):
        doc = json.loads(ih.Client(offline=DIR).get_text("x", "nse_circulars.json"))
        items = news.nse_circular_items(doc)
        self.assertLess(len(items), len(doc["data"]))
        subs = [i["title"] for i in items]
        self.assertIn("NSE circular NSE/FAOP/77549: Introduction of Futures & Options Contracts on Two Individual "
                      "Securities", subs)

    def test_price_bands(self):
        text = ih.Client(offline=DIR).get_text("x", "eq_band_changes_23112026.csv")
        items = news.price_band_items(text, "2026-11-23")
        rushil = [i for i in items if i["symbol"] == "RUSHIL"][0]
        self.assertEqual(rushil["title"], "RUSHIL: price band 20% -> 5% (tightened)")

    def test_surveillance_changes(self):
        c = ih.Client(offline=DIR)
        cur = news.surveillance_snapshot(c.get_json("x", "nse_asm.json"), c.get_json("x", "nse_gsm.json"))
        self.assertEqual(len(cur), 244)
        prev = dict(cur)
        key = sorted(k for k in cur if k.startswith("LT-ASM"))[0]
        del prev[key]
        gone = {"ST-ASM|OLDCO": {"stage": "Stage I", "name": "Old Co"}}
        got = sorted(i["title"] for i in news.surveillance_changes(dict(prev, **gone), cur, "2026-11-23"))
        self.assertEqual(got, ["%s: added to LT-ASM %s" % (key.split("|")[1], cur[key]["stage"]),
                               "OLDCO: exited ST-ASM (was Stage I)"])

    def test_bse_api_shape(self):
        doc = {"Table": [{"NEWSID": "1", "SCRIP_CD": 500325, "SLONGNAME": "Reliance Industries Ltd",
                          "NEWSSUB": "Reliance Industries Ltd - 500325 - Bagging of order", "HEADLINE": "Order worth Rs 500 cr",
                          "SUBCATNAME": "Award of Order / Receipt of Order", "NEWS_DT": "2026-11-23T18:01:00",
                          "ATTACHMENTNAME": "abc.pdf"}]}
        it = news.bse_api_items(doc)[0]
        self.assertEqual((it["tag"], it["link"]), ("orders", "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = ["--offline", DIR, "--now", NOW, "--state", os.path.join(self.d, "state.json"),
                     "--out", os.path.join(self.d, "out"), "--ir-watchlist", os.path.join(self.d, "none.json"),
                     "--no-pdf"]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_a_run_tags_saves_and_dedupes(self):
        code, text, _ = run(self.base)
        self.assertEqual(code, 0)
        for heading in ("== Exchange & Surveillance Triggers", "== Buybacks & Corporate Actions", "== Quarterly Results",
                        "== Order Wins & Reg 30 Catalysts", "== Presentations, Transcripts & Annual Reports"):
            self.assertIn(heading, text)
        self.assertIn("reading BSE's RSS feeds instead", text)
        self.assertIn("RUSHIL: price band 20% -> 5% (tightened)", text)
        self.assertEqual(text.count("Introduction of Futures & Options Contracts on Two Individual Securities"), 1)
        with open(os.path.join(self.d, "out", "latest.json"), encoding="utf-8") as fh:
            latest = json.load(fh)
        self.assertTrue(all(i["link"] for i in latest["items"] if i["source"].startswith("NSE announcements")))
        code, text, _ = run(self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", text)

    def test_filters_and_json(self):
        code, text, _ = run(self.base + ["--dry-run", "--json", "--tags", "exchange"])
        doc = json.loads(text)
        self.assertTrue(doc["items"])
        self.assertEqual({i["tag"] for i in doc["items"]}, {"exchange"})
        self.assertFalse(os.path.exists(os.path.join(self.d, "out")))
        code, _, err = run(self.base + ["--tags", "moon"])
        self.assertEqual(code, 2)
        self.assertIn("unknown tag", err)

    def test_mine(self):
        os.makedirs(os.path.join(self.d, "trader"))
        with open(os.path.join(self.d, "trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "TATASTEEL"}]}, fh)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, _ = run(self.base + ["--dry-run", "--json", "--mine"])
        finally:
            os.chdir(cwd)
        doc = json.loads(text)
        self.assertTrue(doc["items"])
        self.assertEqual({i["symbol"] for i in doc["items"]}, {"TATASTEEL"})


if __name__ == "__main__":
    unittest.main()
