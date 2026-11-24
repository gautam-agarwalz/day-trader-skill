import json
import os
import re
import unittest

from _load import ROOT

# v2, the day-trading pack: the eleven folders the reel's repo card shows
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-pelosi"]
ABOUT = ("11 Claude skills for day trading: scan thousands of charts, track breaking news, time every "
         "entry and exit, and replay every outcome before you risk a dollar.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-pelosi", "trader-risk", "trader-broker", "trader-alerts"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git"))
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "requirements.txt", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill")

    def test_readme_carries_the_reel_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill"))
        for line in (ABOUT,
                     "https://github.com/Jakeschincariol/day-trader-skill install skill",
                     "/plugin marketplace add Jakeschincariol/day-trader-skill",
                     "/plugin install day-trader-skill@day-trader-skill",
                     "## Fine print"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_10(self):
        # requirements.txt: Python 3.10+ (the mcp SDK's floor); no 3.11+ only modules or syntax
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import tomllib", "from typing import Self", "except*", "asyncio.TaskGroup",
                            "asyncio.timeout("):
                    self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_requirements(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
            reqs = [ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()]
        self.assertEqual(reqs, ["mcp>=2.3,<3", "requests>=2.31", "feedparser>=6.0", "pypdf>=4.0", "pdfplumber>=0.10",
                                "pandas>=2.0", "openpyxl>=3.1", "beautifulsoup4>=4.12", "gdeltdoc>=1.5"])
        self.assertFalse(any(r.startswith(("yfinance", "pandas-datareader")) for r in reqs))

    def test_one_india_http_layer_shared_by_smart_money_and_catalysts(self):
        for name, folders in (("india_http.py", ("trader-pelosi", "trader-news", "trader-analyst", "trader-timemachine",
                                                 "trader-scanner")),
                              ("india_results.py", ("trader-analyst", "trader-news")),
                              ("india_catalysts.py", ("trader-news", "trader-timemachine")),
                              ("india_market.py", ("trader-news", "trader-scanner", "trader-timemachine")),
                              ("india_bars.py", ("trader-scanner", "trader-timemachine")),
                              ("india_signals.py", ("trader-alerts", "trader-entry", "trader-exit", "trader-risk",
                                                    "trader-scanner", "trader-timemachine")),
                              ("nse_data.py", ("trader-scanner", "trader-alerts", "trader-timemachine"))):
            with open(os.path.join(ROOT, folders[0], name), encoding="utf-8") as fh:
                ref = fh.read()
            for f in folders[1:]:
                with open(os.path.join(ROOT, f, name), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), ref, "copy %s/%s over %s/%s" % (folders[0], name, f, name))

    def test_no_us_news_or_congress_sources_left(self):
        """The tools and docs point at the Indian sources; only labelled past examples mention the US."""
        bad = ("sec.gov", "edgar", "house clerk", "congress", "stooq", "nasdaqtrader", "sec_user_agent",
               "periodic transaction report", "stock act")
        paths = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, ".claude-plugin", "marketplace.json")]
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if f.endswith((".py", ".md", ".json")):
                    paths.append(os.path.join(ROOT, s, f))
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            text = text.split("## Example: the reel (a past New York run")[0].lower()
            for word in bad:
                self.assertNotIn(word, text, "%s mentions %s" % (os.path.relpath(path, ROOT), word))

    def test_no_language_model_calls_and_no_us_feeds_in_the_indian_modules(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read().lower()
                for bad in ("import anthropic", "from anthropic", "import openai", "api.anthropic.com", "telegram"):
                    self.assertNotIn(bad, src, "%s/%s" % (s, f))
                if s in ("trader-pelosi", "trader-news"):
                    for us in ("sec.gov", "edgar", "house.gov", "yahoo", "periodic transaction"):
                        self.assertNotIn(us, src, "%s/%s" % (s, f))

    def test_one_nse_data_layer_shared_by_scanner_and_alerts(self):
        with open(os.path.join(ROOT, "trader-scanner", "nse_data.py"), encoding="utf-8") as fh:
            a = fh.read()
        with open(os.path.join(ROOT, "trader-alerts", "nse_data.py"), encoding="utf-8") as fh:
            b = fh.read()
        self.assertEqual(a, b, "copy trader-scanner/nse_data.py over trader-alerts/nse_data.py")
        self.assertIn("def fetch_backup_data(", a)
        self.assertIn("# TODO: Plug in Angel One SmartAPI or TradingView backup here", a)

    def test_no_yfinance_or_stooq(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if f.endswith(".py"):
                    with open(os.path.join(folder, f), encoding="utf-8") as fh:
                        src = fh.read().lower()
                    for bad in ("yfinance", "stooq", "pandas_datareader", "nasdaqtrader"):
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_options", False), False)


if __name__ == "__main__":
    unittest.main()
