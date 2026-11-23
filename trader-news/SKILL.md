---
name: trader-news
description: >-
  Indian Corporate Filings & Exchange Catalyst Scanner. Reads official NSE, BSE
  and SEBI feeds (NSE corporate announcements and its RSS feeds for results,
  board meetings, corporate actions, annual reports, circulars and buybacks;
  BSE announcements; SEBI's RSS), plus NSE's ASM / GSM surveillance lists and
  price-band revisions, and sorts every filing into catalyst tags: Quarterly
  Results, Buybacks & Corporate Actions, Order Wins & Reg 30 Catalysts,
  Presentations / Transcripts / Annual Reports, and Exchange & Surveillance
  Triggers. Pulls headline numbers (revenue, profit, EPS, buyback price) from
  the first pages of results and buyback PDFs with regex, links every item to
  the official PDF, flags each once, and never calls a language model. Use when
  the user says "any news on RELIANCE", "what did companies file today",
  "results today", "order wins", "buybacks", "circuit changes", "ASM GSM",
  "F&O inclusion", "investor presentation", "concall transcript", or "monitor
  the filings".
argument-hint: "[--tags results,buyback,orders,documents,exchange] [--mine]"
---

# trader-news

In India, what moves a stock is filed first: a results PDF on NSE and BSE, an
order win under Regulation 30, a buyback letter, a price-band cut, a move into
ASM. This skill reads those filings at the source, tags them, and shows only
what is new. It replaces the old US headline monitor: no Yahoo, no SEC.

Tool in this folder (Python 3.10+, `pip install -r requirements.txt`):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # everything new, all tags
python3 "${CLAUDE_SKILL_DIR}/news.py" --mine                   # only your symbols
python3 "${CLAUDE_SKILL_DIR}/news.py" --tags results,buyback   # some tags
python3 "${CLAUDE_SKILL_DIR}/news.py" --days 3 --all --json    # wider window, seen items too
python3 "${CLAUDE_SKILL_DIR}/news.py" --dry-run                # fetch and parse, write nothing
```

`news.py` shares `india_http.py` (the HTTP client, the dedupe state and the
JSON / CSV writers) with trader-pelosi.

## What it reads and writes

| source | where |
| --- | --- |
| NSE announcements, with NSE's own category | `www.nseindia.in/api/corporate-announcements` |
| NSE circulars | `www.nseindia.in/api/circulars` |
| NSE RSS | `nsearchives.nseindia.com/content/RSS/`: Online_announcements, Financial_Results, Board_Meetings, Corporate_action, Annual_Reports, Circulars, Daily_Buyback |
| BSE | `api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w`; when BSE's firewall refuses (it often refuses cloud IPs), `www.bseindia.com/data/xml/announcements.xml` and `notices.xml` |
| SEBI | `www.sebi.gov.in/sebirss.xml` |
| surveillance | `www.nseindia.in/api/reportASM`, `reportGSM` (changes since the last run), `eq_band_changes_<DDMMYYYY>.csv` |
| company IR pages | optional `trader/ir_watchlist.json`: `{"SYMBOL": "https://company.com/investors"}` |

Writes `trader/catalysts/latest.json` (this run), appends new items to
`trader/catalysts/alerts.csv` (each with its official link), and keeps
`trader/state/catalysts.json` (every flagged item for 90 days, and the ASM /
GSM lists).

## The tags

| tag | what earns it |
| --- | --- |
| `results` | NSE's results categories, "financial results", "un-audited", the results RSS |
| `buyback` | buyback, split, bonus, dividend, record date, rights issue, ex-date, the corporate-action RSS |
| `orders` | NSE's "Bagging/Receiving of orders/contracts", acquisitions, mergers, LoA / LoI, contracts, MoUs, capacity |
| `documents` | investor presentations, con-call updates and transcripts, annual reports (each marked transcript, presentation, annual report or call notice) |
| `exchange` | price band / circuit changes, ASM / GSM / ESM, F&O introductions and exclusions, index changes, MWPL / ban |

A company's own name is ignored by the rules ("GSM Foils" is not a GSM
trigger). Newspaper ads, trading-window notices and share-certificate losses
are dropped. Untagged filings are hidden unless `--all-tags`.

## Numbers from PDFs

For new results and buyback items, the first `--pdf-pages` (2) pages of the PDF
are read and regex copies out revenue, total income, net profit, EPS, the
buyback price and size, as printed. Most results PDFs start with a cover letter
and many carry the table as a scanned image, so often nothing is found; the item
then says so. Never fill a missing number in: open the PDF.

## Steps

1. Run `news.py` (or `--mine` for the user's symbols). Exit code 1 means
   nothing new.
2. Read, in order: exchange triggers on the user's symbols, results and
   buybacks, order wins, then documents.
3. Write a short brief per symbol: what was filed, the numbers if the tool
   found them, the link, and one line on why it matters to a setup (results
   tonight: an overnight gap can jump any stop; a band cut to 5 %: the stock
   can lock in a circuit).
4. If a filing changes the picture, say so before `/trader-entry` and
   `/trader-exit`. Never act on a filing alone.

During the session: `/loop 15m /trader-news`, or a scheduled task before the
open and after the close.

## What it never does

- Never calls a language model or any AI API: tags and numbers are regex.
- Never invents or rounds a number, and never summarises a PDF it did not read.
- Never tells the user to buy or sell because of a filing.
- Never hammers a server: one request per feed, a pause between requests, at
  most `--pdf-max` (8) PDFs per run.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
