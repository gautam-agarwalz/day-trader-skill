---
name: trader-analyst
description: >-
  The Earnings Analyst for NSE stocks. Reads a company's reported results
  straight from NSE: every quarterly filing under SEBI's integrated filing and
  its XBRL (revenue from operations, total income, profit before tax, profit,
  basic and diluted EPS), consolidated by default, and sums up what changed:
  quarter on quarter, against the same quarter last year, margins and the
  trailing twelve months, with the latest filing's link and the next results
  board meeting. Banks and NBFCs too. No language model, no API key. Use when
  the user says "how were TCS's results", "analyze the earnings", "what changed
  this quarter", "is revenue growing", "compare margins", "when are the
  results", or before trading a stock around its results.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Results move Indian stocks more than anything else on the calendar, and the
press release is written to put the best number first. This skill reads the
numbers the company actually filed with NSE, lines them up quarter by quarter,
and says plainly what changed. For a day trader it answers two questions
before a trade: what did the last results say, and are the next ones inside
this trade?

Tool in this folder (Python 3.10+, `pip install -r requirements.txt`):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" TCS                          # 6 quarters, YoY, QoQ, TTM
python3 "${CLAUDE_SKILL_DIR}/facts.py" TCS INFY --quarters 8
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --standalone --json
python3 "${CLAUDE_SKILL_DIR}/facts.py" TCS INFY HDFCBANK --metrics trader/metrics.json
```

`facts.py` shares `india_http.py` (the NSE client) and `india_results.py` (the
results parser, also used by trader-news) with the other Indian tools.

## What it reads and writes

- `www.nseindia.in/api/integrated-filing-results?...type=Integrated Filing- Financials&symbol=X`:
  every results filing since SEBI's integrated filing began (early 2025), with
  its XBRL. Each XBRL gives the quarter and the year to date in rupees.
- `www.nseindia.in/api/results-comparision?symbol=X`: NSE's older feed, quarters
  up to Dec 2024, standalone, used to extend standalone history.
- `www.nseindia.in/api/corporate-board-meetings?symbol=X`: the next board
  meeting that will consider results.
- With `--metrics FILE`: one row per symbol merged into that JSON (see
  `metrics.example.json`).

## The rules

- **Consolidated first.** The group's figures are what the market prices.
  Standalone only with `--standalone` or when a company files no consolidated
  results; the tool says which it used.
- **As filed.** Every number is copied from the company's XBRL. EPS is not
  adjusted for later bonus issues or splits: an EPS that halves after a 1:1
  bonus is the bonus, not a collapse. Say so when it happens.
- **Like with like.** YoY compares a quarter with the same quarter a year
  earlier, in the same consolidated or standalone basis; TTM only when four
  consecutive quarters are there.
- **Banks and NBFCs** file under a banking taxonomy (interest earned stands in
  for revenue); a line a filing does not carry is left blank, never guessed.
- **Numbers, not views.** Never say a stock is a buy because results were good.

## Steps

1. Run `facts.py SYMBOL`. Read the latest quarter's line and the sentences
   under the table (revenue and profit YoY and QoQ, the margin).
2. Open the latest filing's iXBRL link when a number looks odd (a one-off, a
   bonus issue, a restatement) and say what it was.
3. Check the next results date. If it falls inside the planned trade, say so
   before `/trader-entry`: results can gap a stock through any stop.
4. For several companies, `--metrics trader/metrics.json` keeps one row each.

## Example (TCS, filed 8 October 2026)

Quarter to 30 September 2026, consolidated: revenue Rs 73,188 cr (+11.2 % on
the same quarter last year, +1.3 % on the quarter before), profit Rs 13,934 cr
(+14.9 % YoY), EPS 38.37, net margin 19.0 % (18.6 % the quarter before). TTM
revenue Rs 2,83,248 cr, profit Rs 51,858 cr.

## What it never does

- Never calls a language model or any AI API.
- Never fills a missing number, rounds a growth rate up, or predicts the next
  quarter.
- Never hammers NSE: one filing list and at most 12 XBRL files per symbol.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
