# Paper Bot #2: mean reversion + trend following

A second **paper trading** bot, separate from the momentum bot. It runs entirely on
GitHub Actions, so there's nothing to keep running on your phone or computer.

- **Paper only.** The trading address is hardcoded to `https://paper-api.alpaca.markets`.
  There is no live setting. The bot also refuses to start if it's given a live key
  (live keys start with `AK`; paper keys start with `PK`).
- **Starting capital $10,000.** The bot tracks its value from **its own trade history**
  (`data/trades.csv`), not from your Alpaca balance.
- **Backtest first.** The bot won't place a trade until the Backtest workflow has run.

---

## What it trades

| Market | Symbol | Strategy | Bars |
|---|---|---|---|
| S&P 500 | SPY | Mean reversion, closed out before the bell every day | 15-min |
| Nasdaq | QQQ | Mean reversion, closed out before the bell every day | 15-min |
| Gold | GLD | Trend following | Daily |
| Oil | USO | Trend following | Daily |
| Bitcoin | BTC/USD | Mean reversion, smaller size, trades 24/7 | 15-min |

**Mean reversion:** buy when the last finished bar closes 2+ standard deviations below
its 20-bar average. Sell when price gets back to the average, or at the stop
(entry − 2×ATR). For SPY/QQQ, no new trades in the last 50 minutes of the day, and
everything is sold about 35 minutes before the close. These margins are wide
because GitHub sometimes starts scheduled runs several minutes late.

**Trend following:** hold a position while the 20-day EMA is above the 50-day EMA.
Sell when it crosses back below, or at the stop (entry − 3×daily ATR). After a
stop-out the bot waits for a fresh crossover before buying again.

### Risk rules
- **1% risk per trade.** Position size = (1% of account) ÷ (stop distance in ATRs). Volatile
  markets get smaller positions, so the dollar risk stays about the same.
  BTC uses 0.5%.
- **Every trade has a real stop order at Alpaca.** Stocks get a GTC stop order and BTC
  gets a GTC stop-limit order. If a stop can't be placed, the bot sells the position immediately.
- **Size caps:** no position bigger than 33% of the account (15% for BTC), and no margin.
- **Correlation block:** SPY, QQQ and BTC count as "risk-on". Only one of them can be open at
  a time. (You asked for "if long SPY or QQQ, nothing else risk-on". I also made it work
  in reverse, so a BTC position blocks SPY/QQQ.)
- **Max 3 open positions.**
- **Daily stop:** if the account is down 3% on the day (New York date), no new trades
  until tomorrow. Open positions keep their stops and normal exits.

### Safety protections
- Every order has a unique `client_order_id` (e.g. `mr2-SPY-buy-20261120T1445`).
  Alpaca rejects a second order with the same ID, so one signal can't be bought twice.
- The bot checks for its own open orders before buying, and runs never overlap
  (GitHub `concurrency`).
- **It never sells more than it holds:** sell size = the smaller of what its books say and
  what Alpaca actually holds.
- Each run first checks whether any stops filled since the last run, and re-places any
  stop that expired or was cancelled.
- It only touches orders tagged `mr2-`, so it won't touch your momentum bot's orders.
- Errors are logged and that market is skipped. The run then shows a red ✗ in GitHub
  (which emails you), and the error is listed in `data/summary.md`.

---

## Setup from your phone (about 15 minutes)

> GitHub's mobile **app** can't edit secrets. Use your phone's **browser** at github.com.
> If you can't see the **Settings** tab, open the browser menu and tick **"Desktop site"**.

### 1. Merge this code into your main branch
GitHub only runs scheduled workflows from the repo's default branch (usually `main`).
Merge the pull request for this branch, or ask Claude to open one for you.

### 2. Get Alpaca paper keys
1. Log in at **alpaca.markets** and switch to **Paper Trading** (top-left account menu).
2. **Recommended:** create a **new, separate paper account** for this bot and set its
   balance to **$10,000** (account menu → create/open new paper account). This keeps it
   apart from the momentum bot. The bot can share an account if needed, because it keeps
   its own books, but separate accounts are cleaner.
3. On the paper dashboard, find **API Keys** → **Generate New Keys**. Copy the
   **Key** (starts with `PK`) and the **Secret** (only shown once).

### 3. Make a Discord webhook (where the morning/night messages go)
1. Install **Discord** and sign in (a free account is fine).
2. Create a private server: in the left-hand server list tap **+** → **Create My Own** →
   **For me and my friends** → name it e.g. `Trading Bot` → **Create**.
3. Tap the **server name** at the top → **Settings** (gear) → **Integrations** →
   **Webhooks** → **New Webhook**.
4. Tap the new webhook, rename it `Paper Bot`, check the channel is `#general`, then tap
   **Copy Webhook URL**.
   *If you don't see Integrations in the app, open discord.com in your phone browser
   with "Desktop site" on and do the same steps under Server Settings → Integrations.*
5. Turn on notifications: long-press the server icon → **Notifications** → **All Messages**.

Treat the webhook URL like a password: anyone who has it can post to your channel.

### 4. Add the secrets to GitHub
On github.com, open this repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Add these three:

| Name | Value |
|---|---|
| `ALPACA_API_KEY` | your paper Key (starts with `PK`) |
| `ALPACA_SECRET_KEY` | your paper Secret |
| `DISCORD_WEBHOOK_URL` | the webhook URL from step 3 |

### 5. Run the backtest (required before any trading)
Repo → **Actions** tab → **Backtest** → **Run workflow** → **Run workflow**.
It takes a few minutes. Afterwards, open
[`backtest_results/backtest_report.md`](backtest_results/backtest_report.md) to see the
verdict, the bot-vs-SPY table, a year-by-year comparison and the chart. The same report
also appears on the workflow run's summary page.

### 6. That's it
The **Trade** workflow now runs on its own. To check Discord works right away:
**Actions** → **Summary** → **Run workflow** (pick `night`).

---

## Daily use

| Want to... | Do this |
|---|---|
| See how it's doing | Open [`data/summary.md`](data/summary.md) in the GitHub app |
| See every trade | [`data/trades.csv`](data/trades.csv) |
| See the value over time | [`data/performance.csv`](data/performance.csv) and the chart [`data/bot_vs_spy.png`](data/bot_vs_spy.png) |
| Read the bot's log | [`data/bot.log`](data/bot.log) |
| **Pause new trades** | Settings → Secrets and variables → Actions → **Variables** tab → New variable `TRADING_PAUSED` = `true`. Exits and stops keep working. Delete it to resume. |
| Stop everything | Actions → Trade → `•••` → **Disable workflow** (open positions keep their Alpaca stops) |

### Schedule (GitHub uses UTC; these cover New York hours in both summer and winter)
- **Trade:** every 15 minutes on weekdays during market hours (2 minutes after each bar
  finishes), plus every hour around the clock for BTC. Holidays and early closes come from
  Alpaca's market clock, so on those days only BTC trades.
- **Summary to Discord:** about 7:41 AM and 4:37 PM New York time in winter
  (8:41 AM and 5:37 PM in summer). The night message includes the chart.

---

## Files

```
bot/config.py      every setting (symbols, risk, costs) in one place
bot/alpaca.py      tiny Alpaca client: paper URL hardcoded, refuses live keys
bot/indicators.py  SMA, standard deviation, EMA, ATR
bot/strategy.py    entry/exit rules, position sizing, risk checks (shared by live + backtest)
bot/live.py        one trading run
bot/backtest.py    the backtest
bot/ledger.py      the bot's own books (state, trades, performance)
bot/report.py      summary.md, charts, statistics
bot/notify.py      Discord morning/night messages
tests/             offline tests with a fake broker (run on every code change)
.github/workflows/ trade.yml, backtest.yml, summary.yml, tests.yml
```

GitHub's runners forget everything between runs. After each run the bot commits its
files in `data/` back to the repo, so the repo works as the bot's memory.

## Honest notes
- **Expect it to trail buy-and-hold SPY in a rising market.** 1% risk with tight stops
  means small positions, and the account is mostly in cash. Check the backtest report for
  the real numbers; it says plainly whether the bot beat SPY.
- BTC pays about 0.25% in fees each way. Short-term mean reversion on 15-minute bars has to
  overcome roughly 0.6% round-trip costs, which is hard.
- GitHub's scheduler can run late (sometimes 10+ minutes) and occasionally skips runs.
  Broker-side stops protect positions between runs.
- The live bot reads Alpaca's free IEX price feed. The backtest uses the full SIP history,
  so live signals can differ slightly.
- **GitHub Actions minutes:** about 1,300 runs a month. Free on a **public** repo. A
  **private** repo gets 2,000 free minutes a month, shared with your other private repos
  (including the momentum bot's). Your API keys stay safe either way because they're in
  Secrets.

## Running tests on a computer (optional)
```
pip install -r requirements.txt pytest
python -m pytest -q
```
