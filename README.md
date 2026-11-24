# The Day Trader skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

Free, MIT, no signup, no API key. Built for the Indian market (NSE, with BSE
and SEBI feeds): India time, the 9:15 to 3:30 session, the NSE holiday list,
flat by 3:15. The tools are plain Python that read official Indian sources:
NSE's MCP servers (quotes, daily bars, the equity list), NSE's APIs and RSS
feeds (filings, results XBRL, bulk and block deals, insider and SAST
disclosures, pledges, shareholding, surveillance, circulars), BSE and SEBI
feeds and AMFI. 5-minute bars for the intraday tools come from Yahoo
(`SYMBOL.NS`), because NSE publishes no intraday history. No tool calls a
language model.

One scans thousands of NSE charts for setups. Another reads every filing NSE,
BSE and SEBI publish and tags the catalysts (results, buybacks, order wins,
band cuts, ASM). One tracks the smart money (block deals, promoter buying,
pledges, FII / DII flows, mutual fund portfolios). One watches a setup bar by
bar and tells you the moment to get in, and another sets your stop and your
target before you do. And the Time Machine replays the rest of the session
10,000 times from the stock's own recent bars, plus every past day the same
setup fired, and tells you the honest odds. Every trading tool shows the
stock's catalysts and smart-money flags as it works.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> **Looking for the original Trader skill?** Copy an investor's 13F, the
> stock screener, tax lots and rebalancing live in
> [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).
> This repo is its day-trading sequel: the Trader skill v2.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add Jakeschincariol/day-trader-skill
/plugin install day-trader-skill@day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill:trader-scanner` and so on. Copy the folders instead if you want
plain `/trader-scanner`:

```bash
git clone https://github.com/Jakeschincariol/day-trader-skill.git
cp -r day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.10 or newer and `pip install -r requirements.txt`
(the `mcp` SDK for NSE's MCP servers; `requests`, `feedparser`, `pypdf`,
`pdfplumber`, `pandas`, `openpyxl` and `beautifulsoup4` for the filings and
smart-money tools). No API key: every source is public.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans every NSE equity for setups: opening-range breakouts, flags, oversold bounces, each hit marked with its catalysts. |
| `/trader-news` | Filings & Catalyst Scanner | Reads NSE, BSE and SEBI filings and circulars, tags results, buybacks, order wins, presentations and exchange triggers, with exact results numbers from NSE's XBRL. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a dollar: 10,000 simulated sessions plus every past time the setup fired. Its event study measures what followed five years of NSE filings of each kind and turns today's filings into overnight-gap and swing ideas with odds. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to your broker, paper trading by default, never without your yes. |
| `/trader-alerts` | Price & Filing Alerts | Rings when a price, a stop, the opening bell or a new filing on your stock hits. |
| `/trader-analyst` | Earnings Analyst | Reads a company's NSE results filings and sums up what changed. |
| `/trader-pelosi` | Smart Money Tracker | Bulk and block deals, insider (SEBI PIT) and SAST disclosures, pledges, shareholding shifts, FII / DII flows and mutual fund buying. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the close, new filings                         (while it is open)
news / pelosi / analyst                                                        (on a schedule)
```

`/trader-news` writes `trader/catalysts/` and `/trader-pelosi` writes
`trader/smart_money/`. The scanner, entry timer, exit planner, time machine,
risk manager and alerts read both and warn you about the stock you are
trading: results due tonight, results just out, a price band cut to 5 %, an
ASM / GSM entry, an auditor resigning, promoter selling, a pledge invoked,
institutions or insiders buying. Warnings, never blocks.

An example, a day like 23 November 2026 (real NSE data):

1. **`/trader-news`** tagged the day's filings: Dalmia Bharat's board approving
   a buyback at up to Rs 1,850 a share (Rs 700 cr), order wins, results with
   their figures from NSE's XBRL, F&O introductions and price-band cuts.
2. **`/trader-pelosi`**: 94 bulk and block deals, 72 of them same-day round
   trips dropped as noise; Societe Generale and Goldman Sachs bought Lenskart in
   block deals (about Rs 448 cr each) on a day 58 % of its volume was delivered.
3. **`/trader-scanner`** runs the morning scan; each hit carries its catalysts.
4. **`/trader-entry`**, **`/trader-exit`**, **`/trader-timemachine`** time the
   entry, set the stop and target, and replay the odds, each printing the
   stock's catalysts first.
5. **`/trader-budget`**, **`/trader-risk`**, **`/trader-broker`** size it, check
   it against your limits, and wait for your yes.

The skills' SKILL.md files also keep the original reel, a Tesla trade on
Monday 19 October 2026 in New York, as a worked example of each step.

## The tools

```bash
python3 trader-scanner/scan.py universe                                # every NSE equity (series EQ)
python3 trader-scanner/scan.py daily                                   # pre-market setups, NSE daily bars
python3 trader-scanner/scan.py orb --at 10:00                          # opening-range breakouts so far
python3 trader-news/news.py --days 1                                   # filings, circulars, catalysts
python3 trader-news/news.py --mine --tags results,buyback              # only your symbols
python3 trader-news/govt_orders_tracker.py --watch 60                  # govt orders, value vs market cap, lag / loss flags
python3 trader-pelosi/smartmoney.py                                    # deals, insiders, SAST, pledges, flows
python3 trader-scanner/regime.py                                       # risk-on / neutral / risk-off: VIX, breadth, trend, FII
python3 trader-scanner/global_macro_engine.py                          # gold, war, crude, tariffs, Fed, metals: 3 gates
python3 trader-scanner/order_block_scanner.py                          # order blocks, sweep / absorption, anti-trap guards
python3 trader-timemachine/brief.py                                    # every setup through the Time Machine -> the brief
python3 trader-timemachine/brief.py plan 1 --write                     # setup 1 -> trader/plan.json
python3 trader-analyst/facts.py TCS INFY                               # quarters, growth, EPS, next results
python3 trader-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE --every 30                # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-timemachine/events.py backfill --years 5                # 5 years of NSE filings + daily bars (once)
python3 trader-timemachine/events.py study --type order_win            # what followed past order wins
python3 trader-timemachine/events.py suggest                           # overnight-gap / swing / short ideas from today's filings
python3 trader-timemachine/events.py plan 1 --write                    # idea 1 -> trader/plan.json (swing or short)
python3 trader-timemachine/timemachine.py run --event-pool auto        # replay from other stocks' event days
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close, filings
python3 trader-alerts/alerts.py check                                  # rings what hit, once
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures recorded from the real sources: NSE's
filings, deals, SAST, pledge, shareholding and results XBRL, BSE's and SEBI's
feeds, NSE's quotes, plus the original reel's 5-minute bars (re-timed into the
NSE session).

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money. You can lose money. Decide for
yourself, and talk to a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your brokerage tool to "Needs approval" in Claude
(Customize, Connectors, tool permissions), never "Always allow", so Claude's
own "Allow once / Deny" card is a second check. No options, margin, short
selling or leverage unless you turn them on. The broker connectors are US
brokers (Alpaca, IBKR) today: an Indian broker is not connected yet.

**Paper first.** `"paper": true` is the default in `config.example.json`.
Keys live in environment variables, never in the chat.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a halt, a filing. Treat its odds as a reality check,
never as a promise.

**Event odds are history.** The event study counts what followed past NSE
filings of the same kind (from Dec 2020, the start of NSE's full bhavcopy),
with the sample size on every number. An overnight hold can gap through any
stop, and a filing after 3:30 cannot be bought before the next morning's gap.
Shorts (intraday in cash, overnight in stock futures) are off until
`"allow_short": true`.
Overnight (swing) holds are off until you turn them on in `trader/config.json`
(`"swing": {"allow_overnight": true}`); then the exit planner, budget, risk
and alerts handle an exit-by date and size on the gap risk.

**The engine's maps and gates are rules, not insight.** The macro theme map
(gold, war, crude, tariffs, the Fed, metals -> NSE stocks) holds editable
defaults, not findings. Gate 3 is volume: 2x volume is not proof that an
institution traded. Order values, timelines and clients are read by regex from
filings and headlines; a value is only known when the filing states it, and
news is matched to companies by name. GeM and CPPP pages are skipped whenever
they show a CAPTCHA or a login. Intraday odds rest on about 60 sessions of
5-minute bars per stock. Every setup is shown with its Time Machine numbers,
and one that loses money in the simulation says so.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or at a
circuit, a stop order can fill well past its price or not at all. The max loss
shown is the loss at the stop price.

**Disclosures arrive after the trade.** Bulk, block, insider, SAST and pledge
data are public disclosures filed after the fact. The smart-money tracker reads
them fast; there is no inside information here.

**The data.** NSE's MCP servers, APIs and archives are official; Yahoo's
5-minute bars are free and unofficial, can lag, change or rate-limit you.
BSE's API often refuses cloud and foreign IPs, and the tools fall back to
BSE's RSS feeds. Check anything that matters against your broker before you act.

**Costs are counted.** `india_costs.py` prices every round trip: STT, stamp duty, NSE transaction
and SEBI fees, GST, brokerage, DP charges and slippage by liquidity. The Time Machine, the event study,
the brief and the budget report show results after them; the rates are editable in the config's
`costs` block, and your contract note is the final word.

**Prices are split-adjusted, holidays are NSE's.** NSE's daily history is raw, so splits and bonuses
are detected and scaled out before any scan or replay. One holiday calendar (`india_calendar.py`)
serves every tool, refreshed from NSE's own holiday API; trader-risk blocks entries in a year whose
list is not loaded.

**Taxes.** Intraday gains are taxed as speculative business income in India.
Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py, regime.py, global_macro_engine.py, order_block_scanner.py, macro_map.json,
                    nse_data.py, india_signals.py, india_http.py, india_market.py, india_bars.py
trader-news/        SKILL.md, news.py, govt_orders_tracker.py, india_http.py, india_results.py,
                    india_catalysts.py, india_market.py
trader-entry/       SKILL.md, entry.py, india_signals.py, india_calendar.py
trader-exit/        SKILL.md, exit.py, india_signals.py, india_calendar.py
trader-timemachine/ SKILL.md, timemachine.py, events.py, brief.py, india_signals.py, india_catalysts.py,
                    india_http.py, nse_data.py, india_market.py, india_bars.py
trader-risk/        SKILL.md, check.py, india_signals.py, india_calendar.py
trader-budget/      SKILL.md, size.py, india_costs.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json, nse_data.py, india_signals.py, india_calendar.py
trader-analyst/     SKILL.md, facts.py, india_http.py, india_results.py, metrics.example.json
trader-pelosi/      SKILL.md, smartmoney.py, india_http.py
tests/              unittest suite + offline fixtures
requirements.txt    Python packages (Python 3.10+)
```

The shared modules (`nse_data.py`, `india_http.py`, `india_results.py`,
`india_signals.py`, `india_catalysts.py`, `india_market.py`, `india_bars.py`, `india_calendar.py`,
`india_costs.py`) are copied into each folder that uses them so every skill
works on its own; the tests keep the copies identical.

MIT. Made by [Jake Schincariol](https://opusjake.ai).
