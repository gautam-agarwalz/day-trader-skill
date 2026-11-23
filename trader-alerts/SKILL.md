---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop, the opening bell or a new filing
  hits. Turns the trade plan into four alerts in one go (the stop, the
  target, "flat by close" at your flat-by time and the stock's new filings and
  smart-money items from trader-news / trader-pelosi), plus above / below /
  stop / target levels,
  percent moves from the previous close, any time of day, and the NSE
  opening bell, each ringing once so you are not spammed. Knows NSE hours
  (9:15 a.m. to 3:30 p.m.) with the 2026 NSE holidays, works out India time
  (IST) itself, reads live quotes from NSE's official MCP server, and writes
  trader/prices.json for sizing and the risk check. Use when the user says
  "alert me when", "tell me if RELIANCE hits", "alert me on any filing",
  "watch my stop", "watch my trade", "remind me to close by 3:15", "is the
  market open", "when does the market open", "get me a price", or right
  after /trader-exit writes a plan.
argument-hint: "[ticker] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a bracket order the stop-loss and the
take-profit already sit at the broker and do the work; the alerts tell the
user what happened and when it is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close, catalysts from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --catalysts RELIANCE        # every new filing / smart-money item, once
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TSLA --stop 358.32
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TSLA --target 370.04
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by close"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol KO --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote TSLA --write             # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan tickers
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset tsla-plan-stop
```

`--now "2026-10-19 15:15"` pins the clock (India time, IST); `--offline DIR`
reads saved quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the tickers held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the India (IST) clock passed `at` ("15:15") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |
| `catalyst` | trader-news or trader-pelosi saved a new item on `symbol` (a filing, results, a band change, a deal, an insider trade...) | once per item |

A `catalyst` rule reads `trader/catalysts/` and `trader/smart_money/`, so run
`/trader-news` and `/trader-pelosi` on the same schedule as `check` (a `/loop`
or a scheduled task) for it to see anything new.

A rule with `"date"` is watched on that India date only; `--from-plan`
rules carry the plan's date, so yesterday's stop never rings today. A rule
with `"until"` too is watched from `date` to `until`: a swing plan
(`"mode": "swing"`) gets its stop and target from the entry day to its
`exit_by`, an "overnight gap check" at 09:20 each morning after the entry
(compare the open with the stop and re-place the stop: day orders expire),
and a "swing exit day" alarm at 15:15 on `exit_by`, instead of the flat-by. A `time`
rule with `"before_close": true` would move earlier with an early close; the
NSE has none scheduled, so today it changes nothing.

## Quotes

- Live quotes come from NSE's official CM Market Live MCP server
  (`https://mcp.nseindia.in/cmmkt/mcp`, tool `cm_get_stock_quote`) through
  `nse_data.py` and the `mcp` Python SDK: plain tool calls, no language model.
  NSE answers in about 10 seconds a quote and takes them one at a time, so a
  check of several stocks takes a while. Symbols are NSE's own (RELIANCE); a
  `.NS` suffix is accepted.
- A symbol NSE cannot serve (a timeout, a failure, an empty answer) goes to the
  backup hook, `nse_data.fetch_backup_data()`: today the last quote cached in
  `trader/cache/quotes.csv`, marked `"source": "cache"` and keeping its quote
  time, so a stale price stays visibly stale.
- Quotes are fine for alerts and for checking a size, not for timing an order.
  The broker's price is the one that fills. trader-risk only uses a price under
  15 minutes old.

## The NSE clock

Regular session 9:15 a.m. to 3:30 p.m. India time (IST), closed on weekends
and on the NSE's equity-segment trading holidays
(nseindia.com/resources/exchange-communication-holidays):

- 2026: Jan 15 (Maharashtra municipal elections), Jan 26 (Republic Day),
  Mar 3 (Holi), Mar 26 (Ram Navami), Mar 31 (Mahavir Jayanti), Apr 3 (Good
  Friday), Apr 14 (Ambedkar Jayanti), May 1 (Maharashtra Day), May 28 (Bakri
  Id), Jun 26 (Muharram), Sep 14 (Ganesh Chaturthi), Oct 2 (Gandhi Jayanti),
  Oct 20 (Dussehra), Nov 10 (Diwali Balipratipada), Nov 24 (Guru Nanak
  Jayanti), Dec 25 (Christmas).

IST is UTC+5:30 all year (no daylight saving), worked out in the tool, so it
needs no time zone files and ignores the computer's own zone. For any other
year it says the holiday list is unknown. The Diwali Muhurat trading session
is not modelled.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the tickers in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   bracket order has probably filled: check `get_open_position` and record the
   fill with trader-broker. "Flat by close" means close anything still open:
   trader-broker `flatten`, then trader-risk, then the user's yes. Never act
   for them.

## Example: the reel (a past New York run, before the NSE switch)

TSLA on Monday 2026-10-19: entry 364.18 at 10:15 AM, stop 358.32, target
370.04, flat by 15:55. `add --from-plan` writes `tsla-plan-stop`,
`tsla-plan-target` and `tsla-flat-by` for that day. A `check` at 10:53 reads
the real 10:52 quote, 366.48, between the stop and the target:

```
NYSE OPEN (2026-10-19 10:53 New York, regular session).
Nothing hit. 3 rule(s) watched today, TSLA $366.48.
```

At 3:55 PM:

```
ALERT Flat by close: it is 15:55 New York (set for 15:55), the market closes at 16:00. If TSLA is still
open, close it: trader-broker flatten --symbol TSLA builds the sell ticket, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 9:30.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: ticker, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
