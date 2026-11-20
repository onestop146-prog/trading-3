"""Backtest the whole bot on as much Alpaca history as is available.

How the simulation works (kept as close to the live bot as possible):
  * Signals use a bar's CLOSE; the trade happens at the NEXT bar's open
    (that's what the live bot does: it runs right after a bar finishes).
  * Every fill pays slippage, and crypto pays the 0.25% fee, on both sides.
  * Stops: if a bar trades through the stop we sell at the stop price, or at
    the open if price gapped below it (worse fill).
  * SPY/QQQ are sold ~35 minutes before the close every day.
  * GLD/USO use daily bars: signal at the close, trade at the next open.
  * Same portfolio rules as live: 1% ATR risk sizing, max 3 positions,
    correlation block, 3% daily loss halt, starting cash $10,000.
Run:  python -m bot.backtest
"""

import math
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot import config as C
from bot import report
from bot import strategy as S

ET = ZoneInfo("America/New_York")
HISTORY_START = datetime(2015, 1, 1, tzinfo=timezone.utc)  # Alpaca returns what it has


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def download_history(client):
    from bot.live import regular_hours

    end = datetime.now(timezone.utc) - timedelta(minutes=30)  # SIP data must be >15 min old
    mr, trend = {}, {}
    for sym in sorted(C.MEAN_REVERSION_SYMBOLS):
        print(f"Downloading {sym} 15-minute bars...", flush=True)
        df = client.bars(sym, C.MR_TIMEFRAME, HISTORY_START, end, feed=C.BACKTEST_STOCK_FEED)
        mr[sym] = df if S.is_crypto(sym) else regular_hours(df)
    for sym in sorted(C.TREND_SYMBOLS):
        print(f"Downloading {sym} daily bars...", flush=True)
        trend[sym] = client.bars(sym, C.TREND_TIMEFRAME, HISTORY_START, end, feed=C.BACKTEST_STOCK_FEED)
    print("Downloading SPY daily bars (benchmark)...", flush=True)
    spy_daily = client.bars("SPY", "1Day", HISTORY_START, end, feed=C.BACKTEST_STOCK_FEED)
    return mr, trend, spy_daily


def et_dates(index):
    return np.array([t.date() for t in index.tz_convert(ET)])


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
class Backtest:
    def __init__(self):
        self.cash = C.STARTING_CAPITAL
        self.positions = {}      # symbol -> dict
        self.pending = {}        # symbol -> order to execute at the next bar's open
        self.last_price = {}
        self.trades = []
        self.daily_equity = {}   # ET date -> equity at end of that day
        self.day = None
        self.day_start_equity = C.STARTING_CAPITAL
        self.last_equity = C.STARTING_CAPITAL
        self.halted = False
        self.halt_days = 0
        self.wait_for_cross = set()
        self.costs = 0.0         # fees + slippage paid, in dollars
        self.exposure = []       # fraction of the account invested, per day
        self.invested_fraction = 0.0

    # ----------------------------------------------------------- accounting
    def equity(self):
        return self.cash + sum(p["qty"] * self.last_price[s] for s, p in self.positions.items())

    def mark(self, ts):
        """Update account value, the daily loss limit, and the daily equity curve."""
        day = ts.astimezone(ET).date()
        if day != self.day:
            if self.day is not None:
                self.exposure.append(self.invested_fraction)  # yesterday's closing exposure
            self.day, self.day_start_equity, self.halted = day, self.last_equity, False
        eq = self.equity()
        self.invested_fraction = (eq - self.cash) / eq if eq > 0 else 0
        if not self.halted and S.daily_loss_hit(eq, self.day_start_equity):
            self.halted = True
            self.halt_days += 1
        self.daily_equity[day] = eq
        self.last_equity = eq
        return eq

    def buy(self, sym, raw_price, atr_value, ts, planned_qty):
        fill = raw_price * (1 + S.slippage(sym))
        affordable = self.cash / (fill * (1 + S.fee_rate(sym)))
        qty = min(planned_qty, math.floor(affordable * 10_000) / 10_000 if S.is_crypto(sym) else math.floor(affordable))
        if qty <= 0:
            return
        notional = qty * fill
        fee = notional * S.fee_rate(sym)
        self.cash -= notional + fee
        self.costs += fee + qty * raw_price * S.slippage(sym)
        self.positions[sym] = {
            "qty": qty, "entry": fill, "cost_per_unit": (notional + fee) / qty,
            "stop": S.stop_price(sym, fill, atr_value), "entry_time": ts,
            "entry_day": ts.astimezone(ET).date(),
        }
        self.last_price[sym] = raw_price

    def sell(self, sym, raw_price, reason, ts):
        pos = self.positions.pop(sym)
        fill = raw_price * (1 - S.slippage(sym))
        notional = pos["qty"] * fill
        fee = notional * S.fee_rate(sym)
        self.cash += notional - fee
        self.costs += fee + pos["qty"] * raw_price * S.slippage(sym)
        pnl = notional - fee - pos["qty"] * pos["cost_per_unit"]
        self.last_price[sym] = raw_price
        self.trades.append({
            "symbol": sym, "strategy": S.strategy_name(sym),
            "entry_time": pos["entry_time"], "exit_time": ts, "qty": pos["qty"],
            "entry_price": round(pos["entry"], 4), "exit_price": round(fill, 4),
            "pnl": round(pnl, 2), "return_pct": round((fill / pos["entry"] - 1) * 100, 3), "exit_reason": reason,
        })
        if reason == "stop loss" and sym in C.TREND_SYMBOLS:
            self.wait_for_cross.add(sym)

    def busy_symbols(self):
        """Open positions plus buys waiting to execute (both count toward the limits)."""
        return set(self.positions) | {s for s, p in self.pending.items() if p["action"] == "buy"}

    def plan_buy(self, sym, price, atr_value, ts):
        ok, _ = S.can_open(sym, self.busy_symbols(), self.halted)
        if not ok:
            return
        qty = S.position_size(sym, self.last_equity, price, atr_value, self.cash)
        if qty > 0:
            self.pending[sym] = {"action": "buy", "qty": qty, "atr": atr_value, "ts": ts}

    # ------------------------------------------------- mean reversion bars
    def on_mr_bar(self, sym, d, i):
        ts = d["ts"][i]
        o, low, c = d["open"][i], d["low"][i], d["close"][i]
        stock = not S.is_crypto(sym)

        # 1. Execute last bar's decision at this bar's open (if the signal isn't stale).
        order = self.pending.pop(sym, None)
        if order:
            fresh = ts - order["ts"] <= timedelta(minutes=C.MR_BAR_MINUTES + C.MAX_SIGNAL_AGE_MINUTES)
            if order["action"] == "buy" and sym not in self.positions and fresh:
                self.buy(sym, o, order["atr"], ts, order["qty"])
            elif order["action"] == "sell" and sym in self.positions:
                self.sell(sym, o, order["reason"], ts)

        # 2. SPY/QQQ: flat before the close (and never overnight).
        if stock and sym in self.positions:
            pos = self.positions[sym]
            if d["min_to_close"][i] <= C.FLATTEN_MINUTES_BEFORE_CLOSE or pos["entry_day"] != d["date"][i]:
                self.sell(sym, o, "end of day", ts)

        # 3. Stop loss hit during this bar?
        if sym in self.positions and low <= self.positions[sym]["stop"]:
            self.sell(sym, min(o, self.positions[sym]["stop"]), "stop loss", ts)

        self.last_price[sym] = c
        self.mark(ts)

        # 4. Decide at the close of this bar.
        if sym in self.positions:
            if S.mean_reversion_exit(c, d["sma"][i]):
                self.pending[sym] = {"action": "sell", "reason": "back to average", "ts": ts}
        elif S.mean_reversion_entry(d["z"][i], d["atr"][i]):
            minutes_left_after_bar = d["min_to_close"][i] - C.MR_BAR_MINUTES
            if stock and minutes_left_after_bar <= C.NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE:
                return
            self.plan_buy(sym, c, d["atr"][i], ts)

    # ------------------------------------------------------- trend (daily)
    def on_trend_open(self, sym, d, i):
        ts = d["open_ts"][i]
        order = self.pending.pop(sym, None)
        if order:
            if order["action"] == "buy" and sym not in self.positions:
                self.buy(sym, d["open"][i], order["atr"], ts, order["qty"])
            elif order["action"] == "sell" and sym in self.positions:
                self.sell(sym, d["open"][i], order["reason"], ts)
        self.last_price[sym] = d["open"][i]
        self.mark(ts)

    def on_trend_close(self, sym, d, i):
        ts = d["close_ts"][i]
        if sym in self.positions and d["low"][i] <= self.positions[sym]["stop"]:
            self.sell(sym, min(d["open"][i], self.positions[sym]["stop"]), "stop loss", ts)
        self.last_price[sym] = d["close"][i]
        self.mark(ts)

        up = S.trend_is_up(d["ema_fast"][i], d["ema_slow"][i])
        if not up:
            self.wait_for_cross.discard(sym)
            if sym in self.positions:
                self.pending[sym] = {"action": "sell", "reason": "trend ended", "ts": ts}
        elif sym not in self.positions and sym not in self.wait_for_cross:
            self.plan_buy(sym, d["close"][i], d["atr"][i], ts)


def prepare(mr_bars, trend_bars):
    """Add indicators and build one time-ordered list of events for all markets."""
    priority = {s: n for n, s in enumerate(C.SYMBOLS)}
    data, events = {}, []
    for sym, df in mr_bars.items():
        if df.empty:
            continue
        df = S.add_mean_reversion_indicators(df)
        d = {k: df[k].to_numpy() for k in ("open", "high", "low", "close", "sma", "z", "atr")}
        d["ts"] = list(df.index.to_pydatetime())
        d["date"] = et_dates(df.index)
        if S.is_crypto(sym):
            d["min_to_close"] = np.full(len(df), 1e9)
        else:
            # The day's close = end of its last regular-hours bar (handles early closes).
            day_close = pd.Series(df.index, index=df.index).groupby(d["date"]).transform("max")
            d["min_to_close"] = ((day_close - df.index.to_series()).dt.total_seconds() / 60
                                 + C.MR_BAR_MINUTES).to_numpy()
        data[sym] = d
        events += [(t, 1, priority[sym], sym, i) for i, t in enumerate(d["ts"])]

    for sym, df in trend_bars.items():
        if df.empty:
            continue
        df = S.add_trend_indicators(df)
        d = {k: df[k].to_numpy() for k in ("open", "high", "low", "close", "ema_fast", "ema_slow", "atr")}
        dates = et_dates(df.index)
        d["open_ts"] = [datetime.combine(x, time(9, 30), ET).astimezone(timezone.utc) for x in dates]
        d["close_ts"] = [datetime.combine(x, time(16, 0), ET).astimezone(timezone.utc) for x in dates]
        data[sym] = d
        events += [(t, 0, priority[sym], sym, ("open", i)) for i, t in enumerate(d["open_ts"])]
        events += [(t, 2, priority[sym], sym, ("close", i)) for i, t in enumerate(d["close_ts"])]

    events.sort(key=lambda e: (e[0], e[1], e[2]))
    return data, events


def run_backtest(mr_bars, trend_bars):
    data, events = prepare(mr_bars, trend_bars)
    bt = Backtest()
    for _, _, _, sym, i in events:
        if sym in C.TREND_SYMBOLS:
            kind, idx = i
            (bt.on_trend_open if kind == "open" else bt.on_trend_close)(sym, data[sym], idx)
        else:
            bt.on_mr_bar(sym, data[sym], i)
    return bt


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_report(bt, mr_bars, trend_bars, spy_daily):
    os.makedirs(C.BACKTEST_DIR, exist_ok=True)
    bot = pd.Series(bt.daily_equity)
    bot.index = pd.to_datetime(bot.index)

    spy = spy_daily["close"].copy()
    spy.index = pd.to_datetime(et_dates(spy.index))
    spy = spy[(spy.index >= bot.index[0]) & (spy.index <= bot.index[-1])]
    bot_on_spy_days = bot.reindex(bot.index.union(spy.index)).ffill().reindex(spy.index).fillna(C.STARTING_CAPITAL)
    spy_hold = C.STARTING_CAPITAL * spy / spy.iloc[0]

    b, s = report.stats(bot_on_spy_days), report.stats(spy_hold)
    trades = pd.DataFrame(bt.trades)
    trades.to_csv(f"{C.BACKTEST_DIR}/backtest_trades.csv", index=False)
    pd.DataFrame({"bot_equity": bot_on_spy_days.round(2), "spy_hold": spy_hold.round(2)}).to_csv(
        f"{C.BACKTEST_DIR}/backtest_equity.csv", index_label="date")
    report.plot_vs_spy(spy.index, bot_on_spy_days, spy_hold,
                       "Backtest: bot vs holding SPY (starting $10,000, after costs)",
                       f"{C.BACKTEST_DIR}/backtest_chart.png")

    beat = b["total_return"] > s["total_return"]
    m = report.money
    p = report.pct
    lines = [
        "# Backtest report - Paper Bot #2", "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} from Alpaca historical data._", "",
        f"Period: **{spy.index[0]:%Y-%m-%d} to {spy.index[-1]:%Y-%m-%d}** · starting capital {m(C.STARTING_CAPITAL)}", "",
        "## Verdict", "",
    ]
    if beat:
        lines.append(f"The bot **beat** buy-and-hold SPY over this period: {p(b['total_return'])} vs {p(s['total_return'])}.")
    else:
        lines.append(f"**The bot did NOT beat simply holding SPY.** It returned {p(b['total_return'])} "
                     f"vs {p(s['total_return'])} for SPY.")
    if b["max_drawdown"] > s["max_drawdown"]:
        lines.append(f"It did have a smaller worst drawdown ({p(b['max_drawdown'])} vs {p(s['max_drawdown'])}).")
    lines += [
        f"On average only {np.mean(bt.exposure) * 100 if bt.exposure else 0:.0f}% of the account was invested, "
        "because 1%-risk sizing with tight stops keeps positions small - the rest sits in cash.", "",
        "## Bot vs buy-and-hold SPY", "",
        "| | Bot | Hold SPY |", "|---|---|---|",
        f"| Final value | {m(bot_on_spy_days.iloc[-1])} | {m(spy_hold.iloc[-1])} |",
        f"| Total return | {p(b['total_return'])} | {p(s['total_return'])} |",
        f"| Yearly return (CAGR) | {p(b['cagr'])} | {p(s['cagr'])} |",
        f"| Worst drawdown | {p(b['max_drawdown'])} | {p(s['max_drawdown'])} |",
        f"| Sharpe ratio | {b['sharpe']:.2f} | {s['sharpe']:.2f} |", "",
        "![Bot vs SPY](backtest_chart.png)", "",
    ]

    # Year by year
    yearly_bot = bot_on_spy_days.groupby(bot_on_spy_days.index.year).agg(["first", "last"])
    yearly_spy = spy_hold.groupby(spy_hold.index.year).agg(["first", "last"])
    lines += ["## Year by year", "", "| Year | Bot | Hold SPY |", "|---|---|---|"]
    prev_b, prev_s = C.STARTING_CAPITAL, C.STARTING_CAPITAL
    for year in yearly_bot.index:
        yb, ys = yearly_bot.loc[year, "last"], yearly_spy.loc[year, "last"]
        lines.append(f"| {year} | {p(yb / prev_b - 1)} | {p(ys / prev_s - 1)} |")
        prev_b, prev_s = yb, ys
    lines.append("")

    # Trades
    lines += ["## Trades", ""]
    if len(trades):
        lines += [
            f"{len(trades)} trades · win rate {(trades['pnl'] > 0).mean():.0%} · "
            f"average {m(trades['pnl'].mean())} per trade · fees + slippage paid {m(bt.costs)} · "
            f"days halted by the 3% loss rule: {bt.halt_days}", "",
            "| Market | Strategy | Trades | Win rate | Total P&L | Avg P&L |", "|---|---|---|---|---|---|",
        ]
        for sym, g in trades.groupby("symbol"):
            lines.append(f"| {sym} | {g['strategy'].iloc[0]} | {len(g)} | {(g['pnl'] > 0).mean():.0%} | "
                         f"{m(g['pnl'].sum())} | {m(g['pnl'].mean())} |")
        lines += ["", "| Exit reason | Trades | Total P&L |", "|---|---|---|"]
        for reason, g in trades.groupby("exit_reason"):
            lines.append(f"| {reason} | {len(g)} | {m(g['pnl'].sum())} |")
    else:
        lines.append("No trades were made.")
    lines.append("")

    # Data used + honest caveats
    lines += ["## Data used", "", "| Market | Bars | From | To |", "|---|---|---|---|"]
    for sym, df in list(mr_bars.items()) + list(trend_bars.items()):
        if len(df):
            tf = "15-min" if sym in C.MEAN_REVERSION_SYMBOLS else "daily"
            lines.append(f"| {sym} ({tf}) | {len(df):,} | {df.index[0]:%Y-%m-%d} | {df.index[-1]:%Y-%m-%d} |")
    lines += [
        "", "## Assumptions and caveats", "",
        f"- Costs: stock slippage {C.STOCK_SLIPPAGE:.2%} per side; BTC slippage {C.CRYPTO_SLIPPAGE:.2%} "
        f"+ fee {C.CRYPTO_FEE_RATE:.2%} per side. Stock commissions are $0 at Alpaca.",
        "- Trades fill at the next bar's open after a signal. Stops fill at the stop price, or at the open if price gapped through it.",
        "- GLD/USO stops are checked on daily bars, so their timing within a day is approximate.",
        "- Each market only trades once its data exists (BTC history on Alpaca is shorter than stock history).",
        "- Historical SIP data is used here; the live bot reads the free IEX feed, so live signals can differ slightly.",
        "- A backtest is not a promise. Real fills, data gaps and GitHub scheduling delays will make live results worse.",
        "",
    ]
    with open(C.BACKTEST_REPORT, "w") as f:
        f.write("\n".join(lines))
    return b, s


def main():
    from bot.alpaca import AlpacaPaper

    client = AlpacaPaper(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY"))
    mr, trend, spy_daily = download_history(client)
    print("Running simulation...", flush=True)
    bt = run_backtest(mr, trend)
    b, s = build_report(bt, mr, trend, spy_daily)
    print(f"Bot total return {b['total_return']:+.2%} vs SPY {s['total_return']:+.2%}. "
          f"Report: {C.BACKTEST_REPORT}")


if __name__ == "__main__":
    main()
