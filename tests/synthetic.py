"""Fake price data for tests (no network needed)."""

from datetime import time

import numpy as np
import pandas as pd


def random_walk(n, start=100.0, vol=0.002, drift=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(drift, vol, n)))


def to_bars(index, close, seed=0):
    rng = np.random.default_rng(seed + 1)
    close = np.asarray(close)
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.001, len(close))) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 1000.0}, index=index)


def stock_15min(days, start=100.0, seed=0):
    """Regular-hours 15-minute bars (9:30-16:00 New York) on business days."""
    stamps = []
    for d in pd.bdate_range("2024-01-02", periods=days):
        opening = pd.Timestamp.combine(d.date(), time(9, 30)).tz_localize("America/New_York")
        stamps += [opening + pd.Timedelta(minutes=15 * k) for k in range(26)]
    idx = pd.DatetimeIndex(stamps).tz_convert("UTC")
    return to_bars(idx, random_walk(len(idx), start, seed=seed), seed)


def crypto_15min(days, start=40000.0, seed=5):
    idx = pd.date_range("2024-01-02", periods=days * 96, freq="15min", tz="UTC")
    return to_bars(idx, random_walk(len(idx), start, vol=0.004, seed=seed), seed)


def daily(days, start=100.0, drift=0.0005, seed=9):
    idx = pd.bdate_range("2023-06-01", periods=days, tz="UTC") + pd.Timedelta(hours=5)
    return to_bars(idx, random_walk(len(idx), start, vol=0.01, drift=drift, seed=seed), seed)
