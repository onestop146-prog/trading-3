"""The backtest runs end to end and obeys the portfolio rules."""

import pandas as pd

from bot import config as C
from bot.backtest import build_report, run_backtest
from tests import synthetic as syn


def make_data():
    mr = {"SPY": syn.stock_15min(120, 500, 1), "QQQ": syn.stock_15min(120, 400, 2),
          "BTC/USD": syn.crypto_15min(170)}
    tr = {"GLD": syn.daily(300, 200, 0.002, seed=3), "USO": syn.daily(300, 70, seed=4)}
    return mr, tr


def overlaps(trades, symbols=None):
    """Most positions open at the same moment."""
    t = trades if symbols is None else trades[trades["symbol"].isin(symbols)]
    events = [(r.entry_time, 1) for r in t.itertuples()] + [(r.exit_time, -1) for r in t.itertuples()]
    events.sort(key=lambda e: (e[0], e[1]))  # exits before entries at the same time
    peak = cur = 0
    for _, step in events:
        cur += step
        peak = max(peak, cur)
    return peak


def test_backtest_rules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mr, tr = make_data()
    bt = run_backtest(mr, tr)
    trades = pd.DataFrame(bt.trades)
    assert len(trades) > 10
    assert overlaps(trades) <= C.MAX_OPEN_POSITIONS
    assert overlaps(trades, C.RISK_ON_SYMBOLS) <= 1
    # SPY/QQQ are never held overnight
    stocks = trades[trades["symbol"].isin(["SPY", "QQQ"])]
    same_day = [a.tz_convert("America/New_York").date() == b.tz_convert("America/New_York").date()
                for a, b in zip(pd.to_datetime(stocks["entry_time"], utc=True), pd.to_datetime(stocks["exit_time"], utc=True))]
    assert all(same_day)
    # Costs were charged
    assert bt.costs > 0
    # Report files are written
    b, s = build_report(bt, mr, tr, syn.daily(300, 500, seed=7))
    assert (tmp_path / C.BACKTEST_REPORT).exists()
    assert "Verdict" in (tmp_path / C.BACKTEST_REPORT).read_text()
