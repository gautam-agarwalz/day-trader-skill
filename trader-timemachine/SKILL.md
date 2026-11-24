---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of a trade before you risk a
  dollar: 10,000 simulated sessions plus every past time the setup fired.
  Takes the plan from trader-exit (entry, stop, target), rebuilds the rest of
  the day 10,000 times from the stock's own 5-minute bars over the last 60
  days, and re-runs the same rule on every past session. Prints the odds of
  target first, stop first, or neither by the close, where the price sits at
  the close, the average R, the history, and one honest verdict, then hands
  off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:15      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

On a filing day, `--event-pool TYPE` (or `--event-pool auto`, the type of the
stock's latest trader-news / trader-pelosi item) draws the replays from the
day-0 sessions of OTHER stocks that had the same kind of event in Yahoo's
60-day window (from `events.py`'s history, up to 30 stocks, at least 10
sessions or it falls back to the stock's own days and says so). The odds then
answer "what do order-win days do", not "what do ordinary days do". The
history replay stays on the stock's own days.

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-10-19 10:15"` (India time, IST) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 75
   five-minute bars, 9:15 to 3:30 India time (IST). Half days, gaps and today
   are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 3:30 PM, the
   close). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Event study and trade ideas (events.py)

The replays above are ordinary days. A filing day is not ordinary, so
`events.py` measures what followed every past filing of the same kind on NSE
(about five years: NSE's announcements, board-meeting notices, price-band
changes, bulk and block deals, and the daily bhavcopy for prices).

```bash
python3 "${CLAUDE_SKILL_DIR}/events.py" backfill --years 5     # once (hours), resumable: filings first
python3 "${CLAUDE_SKILL_DIR}/events.py" backfill --years 5 --only deals   # later, if NSE's deals service failed
python3 "${CLAUDE_SKILL_DIR}/events.py" update                 # the days since (seconds)
python3 "${CLAUDE_SKILL_DIR}/events.py" types                  # past events of each type
python3 "${CLAUDE_SKILL_DIR}/events.py" study --type order_win
python3 "${CLAUDE_SKILL_DIR}/events.py" study --type results --session during --bucket "up 2-5%"
python3 "${CLAUDE_SKILL_DIR}/events.py" suggest                # ideas from today's /trader-news and /trader-pelosi items
python3 "${CLAUDE_SKILL_DIR}/events.py" plan 1 --write         # idea 1 -> trader/plan.json, swing mode
```

It writes `trader/history/events.sqlite` and, for `suggest`,
`trader/ideas/latest.json` plus `trader/ideas/ideas.csv`. Other flags:
`--cost` (round trip in %, default 0.30 for a delivery trade),
`--min-turnover` (Rs cr a day, default 1), `--sims` (default 10,000),
`study --short` (the short side), `suggest --no-quotes` (skip NSE's live
quotes and use the last close), and `--now "YYYY-MM-DD HH:MM"` to replay a past
moment: `study` and `suggest` then see only the filings before it and the daily
bars before its date, so a replay cannot peek at what happened next.

How it measures. A filing's time decides its session: before 9:15 (pre),
during the session, or after 3:30 / on a holiday (after). Day 0 is the first
session the price could react in. Prices are chained through NSE's previous
close, which NSE adjusts on ex-dates, so a bonus or a split is not a move. A
day-0 close locked at the upper circuit is not buyable. Stocks trading under
Rs 1 cr a day are left out. Costs come off every trade.

It prints, with the sample size on every line: the day-0 gap and move, the 5
days before, the overnight gap (day-0 close to the next open), day 1, 5 and 10
closes, the same against the market (the median NSE stock), the best and worst
point in 10 days, the run-up to a board meeting, and a table by day-0 move
(down >5 % ... up >5 %). Then it replays a grid of rules on every past event:
entry at the day-0 close, the next open or the day-0 open; stop 1 to 3 x the
stock's ATR; target 1 to 3 x the risk; out by day 1 to 10. The rule with the
best lower bound on its average is chosen on events before the last 12 months
and checked on the last 12 months; 10,000 bootstrap resamples (seed 7) give
the range of the average and how often the edge is above zero. A gap through
the stop is filled at the open, and a stop and target in one bar count as the
stop. Fewer than 30 events and a type falls back to its family (CEO change ->
management); still fewer, no odds.

`suggest` takes the items `/trader-news` and `/trader-pelosi` saved, keeps the
ones whose day 0 is today or the next session and that can still be traded
(a filing at 13:00 can be bought before today's close for tomorrow's gap; one
after 3:30 cannot, only from the next open), reads the live day-0 move from
NSE, and studies the same type, session and day-0 move. An idea needs an edge
after costs on the older events AND a positive average on at least 10 trades in
the last 12 months. A rule that looks good but could not be checked on the last
12 months (a short history, a rare type) is listed under skipped, never as an
idea. Each idea has
the entry time, a reference price, the stop and target in rupees, the exit, the
average and the odds, the 5 to 95 % range and the out-of-sample line.
Bearish types (auditor resignations, defaults, pledges created...) and types
whose history leans down are also studied on the short side: an intraday cash
short (filing before the open: short at the day-0 open, cover the same day)
and, for F&O stocks not in the F&O ban period, an overnight short in stock
futures (lot sizes and the ban list from NSE's `fo_mktlots.csv` and
`fo_secban.csv`, refreshed by `update`). The short odds use the cash prices:
the near-month future tracks them, plus a small basis. With no short edge, or
no futures, a bearish filing is printed as a warning.

Schedule it every 15 minutes in the session with `/loop 15m` running
`/trader-news`, then `/trader-pelosi`, then `events.py update` and
`events.py suggest`.

## The funnel (funnel.py): every screener in one scored pipeline

```bash
python3 "${CLAUDE_SKILL_DIR}/funnel.py"                         # score today's candidates -> trader/funnel/
python3 "${CLAUDE_SKILL_DIR}/funnel.py" --refresh regime,macro  # run those tools first (also orders, news, smartmoney)
python3 "${CLAUDE_SKILL_DIR}/funnel.py" review                  # what happened to past signals, by verdict and stage
```

Candidates come from the orders tracker, the filings scanner, the smart money tracker, the macro engine and the
watchlist. Four stages score them; any stage can veto:
1. Universe & safety (0-20): liquidity (Rs 100 cr+ 10, 25 cr+ 8, 10 cr+ 5; under 5 cr vetoed), trade-for-trade
   series BE, ASM / GSM and a 5 % band vetoed, results tonight vetoes overnight holds; regime alignment 0-10.
2. Catalyst & smart money (0-40), which sets the DIRECTION: order wins by size against market cap, cancellations,
   delays and debarments, results 20 % either way, buybacks, smart money, the macro engine's gate-passed list,
   and the event study's out-of-sample verdict for that filing type (+10 / -5).
3. Structure (0-25): the order block scanner, run only on the survivors; an entry must be on the catalyst's side
   (a setup the other way scores 0 and waits); one blocked by the anti-trap guards is vetoed.
4. Time Machine (0-15): the brief's simulation after costs; no edge or too small a sample is vetoed.
PASS at 60 points with no veto, WAIT when only the trigger is missing, FAIL otherwise; every candidate is shown
with its reasons. Each run goes to `trader/journal/signals.csv`; `review` scores the signals from NSE's daily bars
(the signal day's close, days 1, 3 and 5, stop or target first) by verdict and by the stage that vetoed, so you
can see which stage earns its keep. Run it every 5-15 minutes in the session (`/loop 10m`) and `review` after
the close.

## Universal Time Machine & trade brief (brief.py)

```bash
python3 "${CLAUDE_SKILL_DIR}/brief.py"                 # trader/orderblocks + macro + orders -> trader/brief/
python3 "${CLAUDE_SKILL_DIR}/brief.py" plan 1 --write  # setup 1 -> trader/plan.json for exit, budget, risk
python3 "${CLAUDE_SKILL_DIR}/brief.py" plan 1 --futures --write   # its F&O swing-short leg (stock futures)
```

Every candidate from the order block scanner and the macro engine, long or
short, intraday or swing, is simulated before it is shown:
- intraday: `timemachine.time_machine` (10,000 replays of the rest of the
  session from the stock's own 60 days of 5-minute bars): win rate, target /
  stop / open at 15:15, average R, R:R, MAE (median and worst 5 %, in R) and
  the average R for each exit time;
- swing: `events.swing_paths` (every 3-session window of the stock's own NSE
  daily history, drawn 10,000 times, with the overnight gaps as they were; a
  gap through the stop fills at the open): win rate, expectancy after 0.30 %
  costs, MAE, how trades ended, the average for exiting on day 1, 2 or 3; with
  `trader/history/events.sqlite`, also other stocks' days after the same event;
- a long gets both, and the brief names the optimal exit window (intraday by
  HH:MM, or day 2 / day 3);
- every figure is after costs (`india_costs.py`: STT, stamp, exchange, SEBI,
  GST, brokerage, DP, slippage by the stock's turnover; futures costs for the
  F&O short leg), and the swing line adds the after-tax figure (20 % STCG).

The brief: (a) `[EQUITY LONG]` / `[EQUITY SHORT]` setups with their routing tags
(`[SECONDARY F&O]` only where eligible), the order block zone, the 50 %
mean-threshold entry, the confirmation entry, stop, targets and the statistics;
every setup is shown, and one that loses money in the simulation says "no edge in
the Time Machine". (b) `[CAUTION / DO NOT ENTER]`: macro headwinds, EXECUTION
LAG WATCH / ORDER LOSS / DEBARMENT, setups blocked by the anti-trap guards, the
F&O ban, surveillance and results-day flags. The header shows the market regime
(trader-scanner/regime.py) and every setup that fights it is flagged. `plan N` carries the anti-trap
windows into the plan, so trader-risk blocks the same order inside them.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:15` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news and halts can do things those 60 days never did. When
   `/trader-news` or `/trader-pelosi` saved something on the stock, the verdict
   notes say "Event on SYMBOL": these replays are ordinary days, and an event
   day can move further than any of them. Say that out loud.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

For an event idea (`events.py suggest` or `study`): give the entry time, the
stop and target, the exit, then the average, the winners, the 5 to 95 % range
and the number of past trades, the out-of-sample line and the verdict, all as
printed. Say that an overnight hold can gap through any stop, that the
worst-5 % line is the real risk, and that it is history, not a forecast. If
the user wants it, `events.py plan N --write` writes `trader/plan.json` in
swing mode (exit_by, gap risk), and `/trader-budget`, `/trader-risk` and
`/trader-alerts add --from-plan` take it from there. Risk blocks it until the
user turns overnight holds on (`"swing": {"allow_overnight": true}`).

## Example: the reel (a past New York run, before the NSE switch)

TSLA, Monday 2026-10-19. The 15-minute opening range is 354.47 to 362.18.
The 10:10 bar closes above it: entry 364.18 at 10:15 AM (bar 8), stop 358.32
(the range midpoint, rounded down), target 370.04 (1R, risk 5.86 a share).

```
10,000 replays of the rest of the session, 10:15 AM to 4:00 PM (69 five-minute bars), seed 19.
  target first   29.4 %
  stop first     25.5 %
  neither        45.1 %   still open at 4:00 PM
  green          51.7 %   target first, or above the entry at 4:00 PM
  Price at 4:00 PM: median 364.36, 90 % of replays between 355.54 and 373.54.
  Average R: +0.034
History: the same rule on each of those 59 real sessions fired 33 times:
13 target, 15 stop, 5 neither. Average R -0.062.
Close to a coin flip: target first 29.4 % vs stop first 25.5 %.
45.1 % of the replays hit neither by 4:00 PM; the price at 4:00 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 53.6 %. The replays came out close to a coin flip, and the
real history was slightly worse than even. That is the honest answer for this setup, and it is what
the user should hear before `/trader-budget` sizes 8 shares.

## Rules

- Event odds come from past NSE events with the sample size beside them. Never
  quote an average without its n, and never call "edge, not checked out of
  sample" an edge that was checked.
- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
