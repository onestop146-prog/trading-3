"""The live bot's order flow against a fake Alpaca broker (no network)."""

import itertools
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot import config as C
from bot import ledger
from bot.alpaca import AlpacaError
from bot.live import Bot

NOW = datetime(2026, 11, 18, 17, 1, tzinfo=timezone.utc)  # Wednesday 12:01 ET


def mr_bars(drop=False, start=100.0, days=4):
    """15-minute bars ending at NOW (completed); optional sharp drop on the last bar."""
    idx = pd.date_range(end=NOW - timedelta(minutes=16), periods=96 * days, freq="15min", tz="UTC")
    rng = np.random.default_rng(1)
    close = start + rng.normal(0, 0.05, len(idx)).cumsum() * 0.1
    if drop:
        close[-1] = close[-2] - 3.0
    df = pd.DataFrame({"open": close, "high": close + 0.05, "low": close - 0.05, "close": close, "volume": 1.0}, idx)
    df.loc[df.index[-1], "open"] = close[-2]
    df.loc[df.index[-1], "high"] = close[-2] + 0.05
    return df


def daily_bars(up=True):
    idx = pd.bdate_range(end=NOW.date() - timedelta(days=1), periods=120, tz="UTC") + pd.Timedelta(hours=5)
    close = np.linspace(80, 120, 120) if up else np.linspace(120, 80, 120)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, idx)


class FakeBroker:
    def __init__(self, bars, market_open=True, minutes_to_close=240):
        self.bars_by_symbol = bars
        self.market_open = market_open
        self.minutes_to_close = minutes_to_close
        self.orders = {}
        self.held = {}  # symbol without slash -> qty
        self.ids = itertools.count(1)
        self.submitted = []

    # account / clock / positions
    def account(self):
        return {"trading_blocked": False}

    def clock(self):
        return {"is_open": self.market_open,
                "next_close": (NOW + timedelta(minutes=self.minutes_to_close)).isoformat(),
                "next_open": (NOW + timedelta(hours=20)).isoformat()}

    def positions(self):
        return {s: {"symbol": s, "qty": str(q), "current_price": None} for s, q in self.held.items() if q > 0}

    def position_qty(self, symbol):
        return self.held.get(symbol.replace("/", ""), 0.0)

    def last_close(self, symbol):
        return float(self.bars_by_symbol[symbol]["close"].iloc[-1])

    # orders
    def submit_order(self, symbol, qty, side, order_type, tif, client_order_id, stop_price=None, limit_price=None):
        if any(o["client_order_id"] == client_order_id for o in self.orders.values()):
            raise AlpacaError(422, "client_order_id must be unique")
        key = symbol.replace("/", "")
        if side == "sell" and order_type == "market" and float(qty) > self.held.get(key, 0) + 1e-9:
            raise AlpacaError(403, "insufficient qty")
        oid = f"o{next(self.ids)}"
        order = {"id": oid, "client_order_id": client_order_id, "symbol": symbol, "side": side,
                 "type": order_type, "qty": float(qty), "status": "new", "filled_qty": "0",
                 "filled_avg_price": None, "stop_price": stop_price}
        if order_type == "market":
            price = self.last_close(symbol)
            order.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price))
            self.held[key] = self.held.get(key, 0) + (float(qty) if side == "buy" else -float(qty))
        self.orders[oid] = order
        self.submitted.append(order)
        return dict(order)

    def trigger_stop(self, symbol, price):
        for o in self.orders.values():
            if o["symbol"] == symbol and o["type"] in ("stop", "stop_limit") and o["status"] == "new":
                o.update(status="filled", filled_qty=str(o["qty"]), filled_avg_price=str(price))
                self.held[symbol.replace("/", "")] -= o["qty"]

    def get_order(self, oid):
        return dict(self.orders[oid])

    def get_order_by_client_id(self, cid):
        return next((dict(o) for o in self.orders.values() if o["client_order_id"] == cid), None)

    def open_orders(self, symbol):
        return [o for o in self.orders.values()
                if o["symbol"] == symbol and o["status"] == "new" and o["type"] == "market"]

    def cancel_order(self, oid):
        if self.orders[oid]["status"] == "new":
            self.orders[oid]["status"] = "canceled"

    def wait_for_order(self, oid, timeout=30, final_statuses=("filled",)):
        return self.get_order(oid)

    # data
    def bars(self, symbol, timeframe, start, end=None, feed=None):
        df = self.bars_by_symbol[symbol]
        return df[df.index >= start]

    def latest_price(self, symbol):
        return self.last_close(symbol)


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backtest_results").mkdir()
    (tmp_path / C.BACKTEST_REPORT).write_text("report")
    return tmp_path


def all_bars(spy_drop=False, qqq_drop=False, btc_drop=False, trend_up=False):
    return {"SPY": mr_bars(spy_drop, 600), "QQQ": mr_bars(qqq_drop, 500), "BTC/USD": mr_bars(btc_drop, 90_000),
            "GLD": daily_bars(trend_up), "USO": daily_bars(trend_up)}


def buys(broker):
    return [o for o in broker.submitted if o["side"] == "buy"]


def test_buys_on_signal_with_stop_and_no_duplicates():
    broker = FakeBroker(all_bars(spy_drop=True))
    assert Bot(broker, now=NOW).run() == 0
    state = ledger.load_state()
    assert "SPY" in state["positions"]
    stops = [o for o in broker.submitted if o["type"] == "stop"]
    assert len(stops) == 1 and stops[0]["stop_price"] < broker.last_close("SPY")
    # Same bar again (e.g. GitHub ran the job twice): no second buy.
    Bot(broker, now=NOW + timedelta(minutes=2)).run()
    assert len(buys(broker)) == 1
    assert len(ledger.load_trades()) == 1


def test_correlation_block():
    broker = FakeBroker(all_bars(spy_drop=True, qqq_drop=True, btc_drop=True))
    Bot(broker, now=NOW).run()
    assert [o["symbol"] for o in buys(broker)] == ["SPY"]  # QQQ and BTC blocked


def test_trend_entry_and_max_positions():
    broker = FakeBroker(all_bars(spy_drop=True, trend_up=True))
    Bot(broker, now=NOW).run()
    assert sorted(ledger.load_state()["positions"]) == ["GLD", "SPY", "USO"]


def test_stop_fill_is_booked():
    broker = FakeBroker(all_bars(spy_drop=True))
    Bot(broker, now=NOW).run()
    broker.trigger_stop("SPY", 590.0)
    Bot(broker, now=NOW + timedelta(minutes=15)).run()
    trades = ledger.load_trades()
    assert list(trades["side"]) == ["buy", "sell"]
    assert trades.iloc[-1]["reason"] == "stop loss" and trades.iloc[-1]["realized_pnl"] < 0
    assert "SPY" not in ledger.load_state()["positions"]


def test_flatten_before_close_and_never_sell_more_than_held():
    broker = FakeBroker(all_bars(spy_drop=True))
    Bot(broker, now=NOW).run()
    bought = ledger.load_state()["positions"]["SPY"]["qty"]
    broker.held["SPY"] = bought - 2  # pretend 2 shares disappeared at the broker
    broker.minutes_to_close = 20
    Bot(broker, now=NOW + timedelta(minutes=15)).run()
    sells = [o for o in broker.submitted if o["side"] == "sell" and o["type"] == "market"]
    assert len(sells) == 1 and sells[0]["qty"] == bought - 2
    assert ledger.load_trades().iloc[-1]["reason"] == "end of day"
    assert ledger.load_state()["positions"] == {}


def test_daily_loss_halts_new_entries():
    broker = FakeBroker(all_bars(spy_drop=True))
    state = ledger.empty_state()
    state["day"] = {"date": "2026-11-18", "start_equity": 10_400, "halted": False}  # now down >3%
    ledger.save_state(state)
    Bot(broker, now=NOW).run()
    assert buys(broker) == []
    assert ledger.load_state()["day"]["halted"] is True


def test_market_closed_only_btc():
    broker = FakeBroker(all_bars(spy_drop=True, btc_drop=True), market_open=False)
    Bot(broker, now=NOW).run()
    assert [o["symbol"] for o in buys(broker)] == ["BTC/USD"]
    assert any(o["type"] == "stop_limit" for o in broker.submitted)


def test_no_trading_before_backtest(workdir):
    (workdir / C.BACKTEST_REPORT).unlink()
    broker = FakeBroker(all_bars(spy_drop=True))
    assert Bot(broker, now=NOW).run() == 0
    assert broker.submitted == []


def test_performance_and_summary_written():
    broker = FakeBroker(all_bars(spy_drop=True))
    Bot(broker, now=NOW).run()
    perf = ledger.load_performance()
    assert len(perf) == 1 and abs(perf.iloc[0]["equity"] - 10_000) < 1
    assert open(C.SUMMARY_FILE).read().startswith("# Paper Bot #2")


def test_discord_message_and_chart(monkeypatch):
    from bot import notify, report
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    broker = FakeBroker(all_bars(spy_drop=True))
    Bot(broker, now=NOW).run()
    Bot(broker, now=NOW + timedelta(minutes=15)).run()
    text = notify.build_message("night", now=NOW + timedelta(minutes=20))
    assert "Account:" in text and "SPY" in text and len(text) < 2000
    assert report.make_live_chart() == C.CHART_FILE
    assert notify.send(text) is False  # no webhook configured -> just printed
