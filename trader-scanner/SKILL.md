---
name: trader-scanner
description: >-
  Chart Scanner for NSE. Scans thousands of Indian charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the universe of
  every NSE equity (series EQ) from NSE's official MCP server, runs a
  pre-market scan of NSE daily bars (20-day closing-high breakout, bull flag,
  reclaimed the 20-day average, oversold, oversold bounce) and an intraday
  opening-range breakout scan on 5-minute bars, marks each hit with its
  filings and smart-money flags from trader-news and trader-pelosi, then
  writes trader/watchlist.json for trader-news and trader-entry. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of a trading day before /trader-news. Also
  runs the global macro engine (gold, war, crude, US tariffs / China+1, Fed,
  metals; 3 legitimacy gates; long / short / caution lists) and the
  multi-timeframe order block scanner (sweep / absorption confirmation, VWAP,
  anti-trap time and extension guards): use for "macro", "gold surge",
  "crude spike", "war stocks", "order blocks", "smart money entry", "FVG".
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
5,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,INFY  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 5` and `--min-volume` (daily 1,000,000, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (India time, IST) to pin the clock, `--offline PATH`
to read saved files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe` (or the first `daily` / `orb` run): one NSE symbol a line, no `.NS` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:50) |
| `trader/catalysts/`, `trader/smart_money/` | read: each hit gets its filings, smart-money items and flags (`catalysts`, `smart_money`, `flags` in the watchlist) |

## The universe

`universe` reads every NSE capital-market equity in series EQ from NSE's CM
Market Live MCP server (`cm_get_equity_stocks`, paged by symbol prefix, 30 to
60 seconds a page on NSE's side, so a few minutes in all). On 2026-11-21 that
was 2,967 symbols. ETFs listed in series EQ (names like NV20BEES) are in it.
`daily` and `orb` build it on their first run when there is no universe file.

## The daily scan (before the open)

Three months of NSE daily bars per symbol from NSE's CM Bhavcopy MCP server
(`get_stock_history`), about 20 symbols in 2.5 seconds. Liquidity from the
last session: close at least `--min-price` and volume at least
`--min-volume`. A symbol NSE cannot serve (a timeout, a failure, an empty
answer) goes to the backup hook (`nse_data.fetch_backup_data()`, the local
cache in `trader/cache/`) and the printout says which. Today's daily bar is
left out until 3:30 PM.

The setups are computed from daily closes only, as in the reel's `refs/scan.py`
cousin rules:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one (KO on
Oct 16 broke its 20-day closing high, not its 20-day intraday high). Hits are
listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

Timing, live on Tue Oct 20 2026 (a strong morning): 100 liquid names at
10:01 AM took 35 seconds (5 spark requests, 70 v8 charts, 51 of 91 usable
charts broke out). The whole universe at 10:03 AM took about 4 minutes (298
spark requests, then 200 v8 charts): 2,448 usable charts, 1,782 candidates,
the 200 most traded checked, 119 of those broke out. For a fast answer, scan
the daily watchlist or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:35 AM IST, on a
weekend or NSE holiday, after the close, or after `no_entries_after` (an
expired setup) it prints one plain line and writes nothing.

## Data and limits

- Daily bars, quotes and the universe come from NSE's official MCP servers.
  NSE has no intraday bars, so the opening-range scan reads Yahoo's free
  5-minute chart and spark endpoints (a bare NSE symbol is sent as
  `RELIANCE.NS`); they are unofficial and can lag. query2 first, query1 on a
  429 or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 2,880 of 2,967 charts (87
  failed or had no data)". Never round a partial scan up to "the whole market".
- The NSE session (9:15 a.m. to 3:30 p.m. IST) and the 2026 NSE trading
  holidays.
- **Catalysts.** When `/trader-news` and `/trader-pelosi` have run, every hit
  is marked with what they found (results due, a band cut, ASM, a block deal,
  promoter buying or selling) and the printout lists them under the hits.
  Warnings only: a catalyst never removes a hit.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (10:00 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news --mine` (its filings),
   `/trader-pelosi` (smart money), `/trader-analyst SYMBOL` (its last results),
   then `/trader-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example: the reel (a past New York run, before the NSE switch)

Monday Oct 19 2026, scan at 10:15 AM over the reel's 234-ticker wall
(`tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:15" --min-price 0
Fetched 233 of 234 charts (1 failed or had no data); 231 usable; 95 candidates, 95 confirmed with real highs and lows.
42 of 231 charts closed above their opening-range high by 10:15 AM:
   9:50 AM  AMC         $2.97  range 2.90 to 2.96  +0.50% over
  ...
  10:15 AM  TSLA      $364.18  range 354.47 to 362.18  +0.55% over
```

The reel's wall had no price floor (AMC at $2.97 is one of the 42), hence
`--min-price 0`; with the default $5 floor it is 41. TSLA's range is the 9:30
to 9:40 bars, high 362.18, low 354.47; the 10:10 bar closed at 364.18, 0.55 %
over the high, at 10:15 AM. The user picks TSLA and Claude hands it to
`/trader-news TSLA`, then `/trader-entry TSLA`. In the reel the universe was
5,259 US stocks; this tool's rule gives 5,943 on the same files.

The pre-market scan that morning (closes through Fri Oct 16, the reel's daily
charts) tags BLK, CI, XOM and HD a 20-day breakout, CCL a bull flag, F, JPM
and GE a reclaim of the 20-day average, and BAC, MS, RIVN and PLUG oversold,
as the reel did. On closes it also tags KO (breakout), RCL (flag) and PNC (an
oversold bounce).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## Global macro engine (global_macro_engine.py)

```bash
python3 "${CLAUDE_SKILL_DIR}/global_macro_engine.py"               # -> trader/macro/latest.json
python3 "${CLAUDE_SKILL_DIR}/global_macro_engine.py" --themes gold,crude --hours 6
```

Headlines from the GDELT 2.0 DOC API (`gdeltdoc`, or the plain API URL), the
RSS feeds in `macro_map.json` (ET, Moneycontrol, LiveMint, RBI, the Fed, PIB)
and a Google News search per theme, plus company events from
`trader/orders` and `trader/catalysts`. Themes: gold, war / geopolitics,
crude, US tariffs / China+1, the Fed, global metals; copy `macro_map.json` to
`trader/macro_map.json` to change keywords, proxies, thresholds and the stock
lists. The defaults (gold up: long MUTHOOTFIN / MANAPPURAM / TITAN; war or
crude up: long HAL / BEL / MAZDOCK / ONGC, short INDIGO / ASIANPAINT / BPCL /
HPCL; the reverse move flips them) are examples, not findings.

The 3 gates, each with its evidence in the output:
1. Source: a tier-1 domain (exchange, government, central bank) or an
   exchange filing, OR 2 different outlets on the same theme and direction
   within 15 minutes.
2. Size: a proxy past its threshold the expected way (gold GC=F 1.5 %, crude
   CL=F 3 %, copper HG=F 2.5 % ...), or an order >= 5 % of market cap, or
   profit / revenue >= 20 % on last year.
3. Market: 5 to 15 minutes after the news, a 5-minute bar with >= 2.0x the
   20-bar average volume, price moving the expected way. Pending before 5
   minutes, dropped after 15; news outside hours starts at the next 9:15.
   Volume is not proof that an institution traded.

Lists: `[ACTIONABLE LONG]`, `[ACTIONABLE SHORT]`, `[CAUTION / BLOCK ENTRY]`.
Routing is equity first: `[EQUITY INTRADAY LONG]` or `[EQUITY SWING LONG: 2-3+
DAYS]`; every cash short is `[EQUITY INTRADAY SHORT - AUTO SQUARE-OFF 3:15
PM]` (SEBI: cash shorts are squared off the same day), and `[SECONDARY F&O
SWING SHORT OPTION]` is added only for an F&O stock out of the ban period in a
multi-day downtrend.

## Order block scanner (order_block_scanner.py)

```bash
python3 "${CLAUDE_SKILL_DIR}/order_block_scanner.py"                        # symbols from orders, macro, watchlist
python3 "${CLAUDE_SKILL_DIR}/order_block_scanner.py" --symbols KEC,HAL --tf 5m,15m,60m,1d --all
```

An order block is the last opposite-coloured candle before an impulse of 2x
ATR(14) or more, within 3 candles, that closes beyond the last swing high /
low (break of structure) and leaves a 3-candle fair value gap. Zone = that
candle's range, 50 % mean threshold, unmitigated until first revisited. On
5-min, 15-min (resampled) and 1-hour bars from Yahoo, daily from NSE. An entry
needs, on the first revisit: a liquidity sweep and reclaim (a wick beyond the
zone or the swing level closing back inside, volume >= 1.8x) OR an absorption
candle (volume >= 1.8x, narrow body or long wick, close inside the zone in the
top / bottom 30 %); the close on the right side of the session VWAP (intraday)
or the VWAP anchored on the event date (swing); F&O open interest from NSE's
daily F&O bhavcopy as a note only. Anti-trap guards: no intraday entries
09:15-09:30 or 11:45-13:30 or after 14:50, none when the price is more than
1.5x the 14-day ATR from the previous close (intraday) or the anchored VWAP
(swing). Writes `trader/orderblocks/latest.json` with the zone, the 50 %
entry, the stop beyond the far edge, targets and the guards; the brief in
trader-timemachine simulates every setup.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
