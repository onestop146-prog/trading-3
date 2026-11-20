"""Indicators, sizing and risk rules."""

import math

import pandas as pd
import pytest

from bot import config as C
from bot import strategy as S
from bot.alpaca import AlpacaPaper, LiveTradingRefused
from bot.indicators import atr, ema, sma


def test_refuses_live_keys():
    with pytest.raises(LiveTradingRefused):
        AlpacaPaper("AKLIVEKEY123", "secret")


def test_paper_endpoint_is_hardcoded():
    assert C.PAPER_TRADING_URL == "https://paper-api.alpaca.markets"
    assert AlpacaPaper("PKTEST", "secret").base == "https://paper-api.alpaca.markets"


def test_indicators_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 5).iloc[-1] == 3
    assert ema(s, 2).iloc[-1] > 4
    df = pd.DataFrame({"high": [2.0] * 20, "low": [1.0] * 20, "close": [1.5] * 20})
    assert atr(df, 14).iloc[-1] == pytest.approx(1.0)


def test_signals():
    assert S.mean_reversion_entry(-2.1, 1.0)
    assert not S.mean_reversion_entry(-1.9, 1.0)
    assert not S.mean_reversion_entry(float("nan"), 1.0)
    assert S.mean_reversion_exit(101, 100)
    assert S.trend_is_up(2, 1) and not S.trend_is_up(1, 2)


def test_position_size_risk_is_one_percent_when_not_capped():
    # GLD-like: price 100, daily ATR 2, stop = 3 ATR = $6 -> $100 risk / $6 = 16 shares ($1,600)
    qty = S.position_size("GLD", 10_000, 100, 2, 10_000)
    assert qty == 16
    assert qty * 3 * 2 <= 100  # risk <= 1% of the account


def test_position_size_caps_and_cash():
    # Tiny ATR would mean a huge position: capped at 33% of the account.
    assert S.position_size("SPY", 10_000, 100, 0.01, 10_000) == 33
    # Never more than the cash available.
    assert S.position_size("SPY", 10_000, 100, 0.01, 500) == 5
    # BTC smaller cap (15%) and fractional quantity
    q = S.position_size("BTC/USD", 10_000, 50_000, 10, 10_000)
    assert q == pytest.approx(0.03) and q * 50_000 <= 1_500


def test_can_open_rules():
    assert S.can_open("SPY", [], False)[0]
    assert not S.can_open("SPY", [], True)[0]                       # daily loss halt
    assert not S.can_open("QQQ", ["SPY"], False)[0]                 # correlation block
    assert not S.can_open("BTC/USD", ["QQQ"], False)[0]             # correlation block
    assert S.can_open("GLD", ["SPY"], False)[0]                     # gold is not risk-on
    assert not S.can_open("USO", ["SPY", "GLD", "X"], False)[0]     # max 3 positions


def test_daily_loss():
    assert S.daily_loss_hit(9_700, 10_000)
    assert not S.daily_loss_hit(9_701, 10_000)
