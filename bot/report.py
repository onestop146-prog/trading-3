"""summary.md, the bot-vs-SPY chart, and performance statistics."""

import math
import os
from datetime import datetime, timezone

import pandas as pd

from bot import config as C
from bot import ledger


# ---------------------------------------------------------------------------
# Statistics (used by the live summary and the backtest)
# ---------------------------------------------------------------------------
def stats(equity: pd.Series, periods_per_year: int = 252) -> dict:
    """Return, CAGR, max drawdown and Sharpe for a daily equity series."""
    equity = equity.dropna()
    if len(equity) < 2:
        return {"total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
    total = equity.iloc[-1] / equity.iloc[0] - 1
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else 0.0
    drawdown = (equity / equity.cummax() - 1).min()
    rets = equity.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * math.sqrt(periods_per_year) if rets.std() > 0 else 0.0
    return {"total_return": total, "cagr": cagr, "max_drawdown": drawdown, "sharpe": sharpe}


def money(x):
    return f"${x:,.2f}"


def pct(x):
    return f"{x * 100:+.2f}%"


def versus(bot_value, spy_value):
    if abs(bot_value - spy_value) < 1:
        return "even with"
    return "ahead of" if bot_value > spy_value else "behind"


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------
def plot_vs_spy(dates, bot_values, spy_values, title, path):
    """Line chart of the bot's account value against simply holding SPY."""
    import matplotlib
    matplotlib.use("Agg")  # no screen on GitHub's servers
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=120)
    ax.plot(dates, spy_values, color="#9aa0a6", linewidth=1.6, label="Buy & hold SPY")
    ax.plot(dates, bot_values, color="#1a73e8", linewidth=2.0, label="This bot")
    ax.axhline(C.STARTING_CAPITAL, color="#dadce0", linewidth=1, zorder=0)
    ax.set_title(title, fontsize=12, loc="left")
    ax.set_ylabel("Account value ($)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color="#f1f3f4")
    ax.legend(frameon=False, loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def make_live_chart():
    perf = ledger.load_performance()
    perf = perf[pd.to_numeric(perf["spy_hold_value"], errors="coerce").notna()]
    if len(perf) < 2:
        return None
    dates = pd.to_datetime(perf["time_utc"])
    plot_vs_spy(dates, perf["equity"].astype(float), perf["spy_hold_value"].astype(float),
                "Paper bot vs holding SPY (starting $10,000)", C.CHART_FILE)
    return C.CHART_FILE


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------
def write_summary(errors=None):
    state = ledger.load_state() if os.path.exists(C.STATE_FILE) else ledger.empty_state()
    perf = ledger.load_performance()
    trades = ledger.load_trades()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = ["# Paper Bot #2 - Summary", "", f"_Updated {now}. Paper money only._", ""]
    if len(perf):
        last = perf.iloc[-1]
        equity = float(last["equity"])
        lines += [
            "| | Value |", "|---|---|",
            f"| Account value | **{money(equity)}** ({pct(equity / C.STARTING_CAPITAL - 1)}) |",
            f"| Today | {float(last['day_pnl_pct']):+.2f}% |",
        ]
        if str(last.get("spy_hold_value", "")) not in ("", "nan"):
            spy_val = float(last["spy_hold_value"])
            lines.append(f"| Just holding SPY | {money(spy_val)} ({pct(spy_val / C.STARTING_CAPITAL - 1)}) - "
                         f"bot is **{versus(equity, spy_val)}** SPY |")
        lines.append(f"| Trading halted today | {'YES (down 3%)' if state['day'].get('halted') else 'no'} |")
        lines.append("")

    lines += ["## Open positions", ""]
    if state["positions"]:
        lines += ["| Symbol | Qty | Entry | Stop | Strategy |", "|---|---|---|---|---|"]
        for sym, p in state["positions"].items():
            lines.append(f"| {sym} | {p['qty']:g} | {money(p['entry_price'])} | {money(p['stop_price'])} | {p['strategy']} |")
    else:
        lines.append("None - all cash.")
    lines.append("")

    sells = trades[trades["side"] == "sell"] if len(trades) else trades
    if len(sells):
        pnl = pd.to_numeric(sells["realized_pnl"], errors="coerce")
        lines += ["## Closed trades", "",
                  f"{len(sells)} closed, win rate {(pnl > 0).mean():.0%}, total realized {money(pnl.sum())}.", "",
                  "| Symbol | Trades | Realized P&L |", "|---|---|---|"]
        for sym, grp in sells.assign(pnl=pnl).groupby("symbol"):
            lines.append(f"| {sym} | {len(grp)} | {money(grp['pnl'].sum())} |")
        lines.append("")

    if len(trades):
        lines += ["## Last 10 fills", "", "| Time (UTC) | Side | Symbol | Qty | Price | Reason |", "|---|---|---|---|---|---|"]
        for _, t in trades.tail(10).iloc[::-1].iterrows():
            lines.append(f"| {t['time_utc']} | {t['side']} | {t['symbol']} | {t['qty']:g} | {money(float(t['price']))} | {t['reason']} |")
        lines.append("")

    if errors:
        lines += ["## Errors in the last run", ""] + [f"- {e}" for e in errors] + [""]

    lines += ["Chart: [bot_vs_spy.png](bot_vs_spy.png) (updated with each morning/night summary) · "
              f"Backtest: [report](../{C.BACKTEST_REPORT})", ""]
    os.makedirs(C.DATA_DIR, exist_ok=True)
    with open(C.SUMMARY_FILE, "w") as f:
        f.write("\n".join(lines))
