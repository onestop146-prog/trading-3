"""The bot's own books: state.json, trades.csv, performance.csv.

The account value is computed ONLY from the bot's own trade history:
    equity = $10,000 + sum of every trade's cash flow + value of open positions
so it works even if the Alpaca paper account holds other money or positions.
"""

import csv
import json
import os

import pandas as pd

from bot import config as C

TRADE_COLUMNS = [
    "time_utc", "symbol", "side", "qty", "price", "fee", "cash_flow",
    "realized_pnl", "reason", "strategy", "order_id", "client_order_id",
]
PERFORMANCE_COLUMNS = [
    "time_utc", "equity", "cash", "positions_value", "open_positions",
    "day_pnl_pct", "total_return_pct", "spy_price", "spy_hold_value", "halted",
]


def empty_state() -> dict:
    return {
        "positions": {},                 # symbol -> details of the open position
        "day": {"date": None, "start_equity": C.STARTING_CAPITAL, "halted": False},
        "trend_wait_for_new_cross": {},  # symbol -> True after a stop-out
        "spy_start_price": None,         # for the "just hold SPY" comparison
    }


def load_state() -> dict:
    if not os.path.exists(C.STATE_FILE):
        return empty_state()
    with open(C.STATE_FILE) as f:
        state = json.load(f)
    for key, value in empty_state().items():
        state.setdefault(key, value)
    return state


def save_state(state: dict) -> None:
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = C.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, C.STATE_FILE)  # atomic: never leaves a half-written file


def _append_row(path, columns, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if new_file:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in columns})


def append_trade(row: dict) -> None:
    _append_row(C.TRADES_FILE, TRADE_COLUMNS, row)


def append_performance(row: dict) -> None:
    _append_row(C.PERFORMANCE_FILE, PERFORMANCE_COLUMNS, row)


def load_trades() -> pd.DataFrame:
    if not os.path.exists(C.TRADES_FILE) or os.path.getsize(C.TRADES_FILE) == 0:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    return pd.read_csv(C.TRADES_FILE)


def load_performance() -> pd.DataFrame:
    if not os.path.exists(C.PERFORMANCE_FILE) or os.path.getsize(C.PERFORMANCE_FILE) == 0:
        return pd.DataFrame(columns=PERFORMANCE_COLUMNS)
    return pd.read_csv(C.PERFORMANCE_FILE)


def cash_balance() -> float:
    """Starting capital plus every buy (negative) and sell (positive) cash flow."""
    trades = load_trades()
    return C.STARTING_CAPITAL + float(pd.to_numeric(trades["cash_flow"], errors="coerce").fillna(0).sum())


def positions_value(state: dict, prices: dict) -> float:
    total = 0.0
    for symbol, pos in state["positions"].items():
        price = prices.get(symbol) or pos["entry_price"]
        total += pos["qty"] * price
    return total
