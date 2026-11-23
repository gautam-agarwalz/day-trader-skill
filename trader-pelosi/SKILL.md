---
name: trader-pelosi
description: >-
  Indian Smart Money & Institutional Tracker. Reads official NSE and AMFI data
  to show who is really buying and selling Indian stocks: NSE bulk and block
  deals checked against the day's delivery % (intraday round trips filtered
  out), SEBI PIT insider-trading disclosures (promoter, director and KMP
  open-market buys highlighted), SAST takeover disclosures (5 % crossings),
  promoter pledge creation and release, quarterly shareholding shifts (FII,
  DII, promoter), the day's FII / DII cash flows, and mutual fund monthly
  portfolios (fresh buys and stake increases). Flags each item once, saves
  JSON and CSV locally, and never calls a language model. Use when the user
  says "smart money", "who is buying", "bulk deals", "block deals", "insider
  buying", "promoter buying", "pledge", "SAST", "FII DII data", "shareholding
  pattern", "mutual fund buying", or "run the Pelosi skill".
argument-hint: "[--only deals,insider,sast,pledge,shareholding,fiidii,amfi]"
---

# trader-pelosi

The folder keeps its old name so installs and links keep working. The US
tracker it held is gone: this is now an Indian smart money and
institutional tracker. Everything it reads is a public disclosure from NSE or
AMFI. Fast, not early: a disclosure arrives after the trade, sometimes days
after. There is no inside information here. Say that every time.

Tool in this folder (Python 3.10+, `pip install -r requirements.txt`):

```bash
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py"                     # every source, new items only
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py" --only deals,fiidii  # some sources
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py" --days 7 --all       # wider window, seen items too
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py" --dry-run --json     # fetch and parse, write nothing
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py" amfi-links           # AMFI's list of fund-house portfolio pages
python3 "${CLAUDE_SKILL_DIR}/smartmoney.py" amfi --file oct.xlsx --amc "PPFAS MF" --month 2026-10
```

`smartmoney.py` shares `india_http.py` (the HTTP client, the dedupe state and
the JSON / CSV writers) with trader-news.

## What it reads and writes

| source | where | what is highlighted |
| --- | --- | --- |
| deals | `www.nseindia.in/api/snapshot-capital-market-largedeal`, delivery % from `nsearchives.nseindia.com` `sec_bhavdata_full_<DDMMYYYY>.csv` | block deals, institutions, big high-delivery deals |
| insider | NSE PIT API, NSE `Insider_Trading.xml` RSS (each XBRL read), announcements citing Reg 7 | promoter / director / KMP open-market buys |
| sast | `www.nseindia.in/api/corporate-sast-reg29` | open-market 5 % crossings, promoter buys of 0.1 % or more, institutional buys |
| pledge | `www.nseindia.in/api/corporate-pledgedata` against the last run, Reg 31 announcements | new, bigger, released and revoked pledges |
| shareholding | `Shareholding_Pattern.xml` RSS (XBRL: promoter, FII, DII, public), NSE shareholding master | a 1 pp move in promoter, FII or DII stake |
| fiidii | `www.nseindia.in/api/fiidiiTradeReact` (provisional, all exchanges) | a net day of Rs 1,000 cr or more |
| amfi | AMFI's portfolio-disclosure directory; each fund house's own monthly Excel | a stock new to the fund house, or 10 % more shares |

Writes `trader/smart_money/latest.json` (this run), appends new items to
`trader/smart_money/alerts.csv`, and keeps `trader/state/smart_money.json`
(every flagged item for 90 days, plus the pledge, shareholding and fund
snapshots the diffs need). Mutual fund files to read every run go in
`trader/amfi_portfolios.json`: `[{"amc": "...", "month": "2026-10", "file": "..."}]`
(or `"url"`).

## The rules

- **Disclosures are leads, not signals.** Read the filing behind an item
  before saying what it means. Quote the source and link it.
- **Intraday is not conviction.** A client that bought and sold the same stock
  the same day, or a trading desk on a low-delivery day, is filtered out as
  noise. Say how many were filtered (the tool prints it).
- **Promoter buying through the market is the strongest insider signal;**
  ESOP allotments, gifts, inter-se transfers and inheritances are not.
- **First runs are baselines.** Pledge changes, promoter-stake shifts and fund
  portfolio changes need a previous snapshot; the first run says so.
- **Stale feeds are said out loud.** The notes give each feed's newest date
  (NSE's Insider Trading RSS has been stale since May 2026; its PIT API returned
  nothing in testing). Never present an old item as new.
- **No predictions.** Report what was disclosed. Never say what a stock will do.

## Steps

1. Run `smartmoney.py`. Exit code 1 means nothing new.
2. Read the highlighted items first (marked `*`), then anything on one of the
   user's symbols (marked `!`, from the watchlist, the plan and the portfolio).
3. For each one that matters, open the link and give a short line: who, what,
   how big, the delivery % for a deal, the holding before and after.
4. If a disclosure changes the picture for a planned trade (a promoter
   selling, a pledge invoked), say so before `/trader-entry`. Never act on one
   alone.

Schedule it after the close (deals, FII / DII and delivery are final by the
evening) with a Claude scheduled task, or run `/loop 60m /trader-pelosi`.

## Example: a real run (2026-11-23)

94 bulk and block deals; 72 were same-day round trips and were dropped. Kept:
Societe Generale ODI and Goldman Sachs bought 61,76,351 Lenskart shares each in
block deals at Rs 725 (about Rs 448 cr each) while Steadview Capital sold, on a
day 58 % of Lenskart's volume was delivered. DII net sold Rs 2,760 cr and
FII / FPI net bought Rs 5,673 cr (provisional). 121 SAST disclosures, 27
highlighted, among them Nippon India Mutual Fund crossing 5 % of Swiggy
(4.99 % to 5.17 %).

## What it never does

- Never calls a language model or any AI API.
- Never invents a number: every figure is copied from the exchange's own data.
- Never hammers a server: one request per feed, a pause between requests, and
  at most `--max-xbrl` (25) XBRL files per feed per run.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the brokerage tool is set to "Needs approval" in Claude
  (Customize > Connectors > tool permissions) so Claude's own "Allow once /
  Deny" card appears too. Never "Always allow" an order tool.
- No options, margin, shorting or leverage unless the config allows it and
  the user confirms.
- Bulk, block, insider, SAST and pledge data are PUBLIC disclosures that
  arrive after the trade. This skill reads them fast; it has no inside
  information, and it says so.
