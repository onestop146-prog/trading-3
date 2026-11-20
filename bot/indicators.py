"""Technical indicators. Each takes a pandas Series/DataFrame and returns a Series."""

import pandas as pd


def sma(close: pd.Series, n: int) -> pd.Series:
    """Simple moving average of the last n bars."""
    return close.rolling(n).mean()


def rolling_std(close: pd.Series, n: int) -> pd.Series:
    """Standard deviation of the last n bars."""
    return close.rolling(n).std()


def ema(close: pd.Series, n: int) -> pd.Series:
    """Exponential moving average (recent bars count more)."""
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Average True Range: the typical size of one bar's move (volatility).

    True range = the biggest of: high-low, |high - previous close|, |low - previous close|.
    """
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's smoothing, the standard way ATR is calculated
    return true_range.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
