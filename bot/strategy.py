"""Trading rules shared by the live bot AND the backtest, so both trade the same way."""

import math

import pandas as pd

from bot import config as C
from bot.indicators import atr, ema, rolling_std, sma


# ---------------------------------------------------------------------------
# Small helpers about each symbol
# ---------------------------------------------------------------------------
def is_crypto(symbol: str) -> bool:
    return symbol in C.CRYPTO_SYMBOLS


def strategy_name(symbol: str) -> str:
    return "mean_reversion" if symbol in C.MEAN_REVERSION_SYMBOLS else "trend"


def risk_per_trade(symbol: str) -> float:
    return C.BTC_RISK_PER_TRADE if is_crypto(symbol) else C.RISK_PER_TRADE


def max_position_pct(symbol: str) -> float:
    return C.BTC_MAX_POSITION_PCT if is_crypto(symbol) else C.MAX_POSITION_PCT


def fee_rate(symbol: str) -> float:
    return C.CRYPTO_FEE_RATE if is_crypto(symbol) else C.STOCK_FEE_RATE


def slippage(symbol: str) -> float:
    return C.CRYPTO_SLIPPAGE if is_crypto(symbol) else C.STOCK_SLIPPAGE


def stop_atr_multiple(symbol: str) -> float:
    return C.MR_STOP_ATR if symbol in C.MEAN_REVERSION_SYMBOLS else C.TREND_STOP_ATR


# ---------------------------------------------------------------------------
# Indicators each strategy needs
# ---------------------------------------------------------------------------
def add_mean_reversion_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma"] = sma(df["close"], C.MR_LOOKBACK)
    df["std"] = rolling_std(df["close"], C.MR_LOOKBACK)
    # z-score: how many standard deviations the close is from its average
    df["z"] = (df["close"] - df["sma"]) / df["std"]
    df["atr"] = atr(df, C.ATR_PERIOD)
    return df


def add_trend_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], C.TREND_FAST)
    df["ema_slow"] = ema(df["close"], C.TREND_SLOW)
    df["atr"] = atr(df, C.ATR_PERIOD)
    return df


# ---------------------------------------------------------------------------
# Signals (all use a COMPLETED bar, never a bar that is still forming)
# ---------------------------------------------------------------------------
def _ok(*values) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in values)


def mean_reversion_entry(z, atr_value) -> bool:
    """Buy when the close is 2+ standard deviations below the 20-bar average."""
    return _ok(z, atr_value) and atr_value > 0 and z <= C.MR_ENTRY_Z


def mean_reversion_exit(close, sma_value) -> bool:
    """Take profit once price is back at the moving average."""
    return _ok(close, sma_value) and close >= sma_value


def trend_is_up(ema_fast, ema_slow) -> bool:
    """Long while the 20 EMA is above the 50 EMA."""
    return _ok(ema_fast, ema_slow) and ema_fast > ema_slow


# ---------------------------------------------------------------------------
# Position sizing and risk checks
# ---------------------------------------------------------------------------
def stop_price(symbol: str, entry_price: float, atr_value: float) -> float:
    return round(entry_price - stop_atr_multiple(symbol) * atr_value, 2)


def position_size(symbol: str, equity: float, price: float, atr_value: float, cash: float) -> float:
    """How much to buy so that hitting the stop loses ~1% of the account.

    risk in dollars = equity x 1%
    risk per share  = stop distance = ATR x multiple
    quantity        = risk in dollars / risk per share
    Then capped so one position is never too big and we never use margin.
    """
    if not _ok(price, atr_value) or price <= 0 or atr_value <= 0 or equity <= 0:
        return 0
    risk_dollars = equity * risk_per_trade(symbol)
    stop_distance = stop_atr_multiple(symbol) * atr_value
    qty = risk_dollars / stop_distance

    # Caps: max % of the account per position, and never more than the cash we have.
    max_dollars = min(equity * max_position_pct(symbol), cash / (1 + fee_rate(symbol)))
    qty = min(qty, max_dollars / price)

    if is_crypto(symbol):
        qty = math.floor(qty * 10_000) / 10_000  # 4 decimals of BTC
        return qty if qty * price >= 10 else 0   # skip dust orders under $10
    return math.floor(qty)  # whole shares only (needed for overnight stop orders)


def can_open(symbol: str, open_symbols, halted: bool) -> tuple[bool, str]:
    """Portfolio-level rules. Returns (allowed, reason_if_not)."""
    open_symbols = set(open_symbols)
    if halted:
        return False, "daily loss limit hit, no new trades today"
    if symbol in open_symbols:
        return False, "already in this position"
    if len(open_symbols) >= C.MAX_OPEN_POSITIONS:
        return False, f"already at max {C.MAX_OPEN_POSITIONS} open positions"
    if symbol in C.RISK_ON_SYMBOLS and open_symbols & C.RISK_ON_SYMBOLS:
        held = ", ".join(sorted(open_symbols & C.RISK_ON_SYMBOLS))
        return False, f"correlation block: already long risk-on {held}"
    return True, ""


def daily_loss_hit(equity: float, day_start_equity: float) -> bool:
    return day_start_equity > 0 and equity <= day_start_equity * (1 - C.DAILY_LOSS_LIMIT)
