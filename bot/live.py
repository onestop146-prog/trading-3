"""One run of the paper trading bot. GitHub Actions starts this every 15 minutes
during market hours, and every hour otherwise (BTC only).

Order of work in each run:
  1. Safety checks (paper only, backtest exists, which markets are open)
  2. Download recent bars for each market we can trade right now
  3. Reconcile: did any stop losses fill since the last run?
  4. Work out the account value and the daily loss limit
  5. Exits first (end of day, back to average, trend over, stop check)
  6. Entries (signal + all risk rules)
  7. Save trades.csv / performance.csv / summary.md / state.json
Every market is handled inside try/except: an error is logged and that market
is skipped, the rest of the run carries on.
"""

import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from bot import config as C
from bot import ledger, report
from bot import strategy as S
from bot.alpaca import OPEN_ORDER_STATUSES, AlpacaPaper

ET = ZoneInfo("America/New_York")
log = logging.getLogger("bot")


def setup_logging():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(C.LOG_FILE)):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def trim_log(max_lines=3000):
    """Keep data/bot.log small enough to read on a phone."""
    if os.path.exists(C.LOG_FILE):
        with open(C.LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(C.LOG_FILE, "w") as f:
                f.writelines(lines[-max_lines:])


def clean(symbol):
    return symbol.replace("/", "")


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def regular_hours(df):
    """Keep only 9:30-16:00 New York time bars (drops pre/after-market)."""
    if df.empty:
        return df
    t = df.index.tz_convert(ET)
    minutes = t.hour * 60 + t.minute
    return df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]


class Bot:
    def __init__(self, client, now=None, trigger="schedule"):
        self.client = client
        self.now = now or datetime.now(timezone.utc)
        self.trigger = trigger
        self.today = self.now.astimezone(ET).date().isoformat()
        self.errors = []
        self.state = None
        self.bars = {}
        self.prices = {}
        self.order_seq = 0

    # ------------------------------------------------------------ utilities
    def error(self, msg):
        log.error(msg)
        self.errors.append(msg)

    def record_trade(self, symbol, side, qty, price, fee, cash_flow, reason, order, realized=""):
        ledger.append_trade({
            "time_utc": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol, "side": side, "qty": round(qty, 8), "price": round(price, 4),
            "fee": round(fee, 4), "cash_flow": round(cash_flow, 4),
            "realized_pnl": "" if realized == "" else round(realized, 2),
            "reason": reason, "strategy": S.strategy_name(symbol),
            "order_id": order.get("id", ""), "client_order_id": order.get("client_order_id", ""),
        })
        log.info(f"TRADE {side.upper()} {qty} {symbol} @ {price:.2f} ({reason})")

    def record_sell(self, symbol, qty, price, reason, order):
        """Book a sale against the position and return realized profit."""
        pos = self.state["positions"][symbol]
        notional = qty * price
        fee = notional * S.fee_rate(symbol)
        realized = notional - fee - qty * pos["cost_per_unit"]
        self.record_trade(symbol, "sell", qty, price, fee, notional - fee, reason, order, realized)
        pos["qty"] = round(pos["qty"] - qty, 8)
        if reason == "stop loss" and symbol in C.TREND_SYMBOLS:
            # After being stopped out of a trend trade, wait for a fresh crossover.
            self.state["trend_wait_for_new_cross"][symbol] = True
        return realized

    def time_key(self):
        return self.now.strftime("%Y%m%dT%H%M")

    # --------------------------------------------------------------- data
    def load_bars(self, symbol, market_open):
        """Recent bars with indicators. Drops the bar that is still forming."""
        if symbol in C.MEAN_REVERSION_SYMBOLS:
            days = 3 if S.is_crypto(symbol) else 10
            df = self.client.bars(symbol, C.MR_TIMEFRAME, self.now - timedelta(days=days))
            if not S.is_crypto(symbol):
                df = regular_hours(df)
            df = df[df.index + timedelta(minutes=C.MR_BAR_MINUTES) <= self.now]  # completed only
            return S.add_mean_reversion_indicators(df)
        # Trend: daily bars, excluding today's unfinished bar
        df = self.client.bars(symbol, C.TREND_TIMEFRAME, self.now - timedelta(days=420))
        df = df[[d.astimezone(ET).date().isoformat() < self.today for d in df.index]]
        return S.add_trend_indicators(df)

    def signal_is_fresh(self, symbol):
        df = self.bars[symbol]
        if df.empty:
            return False
        if symbol in C.TREND_SYMBOLS:
            return (self.now - df.index[-1]) < timedelta(days=6)
        bar_end = df.index[-1] + timedelta(minutes=C.MR_BAR_MINUTES)
        return (self.now - bar_end) <= timedelta(minutes=C.MAX_SIGNAL_AGE_MINUTES)

    # ------------------------------------------------------- stop orders
    def place_stop(self, symbol, qty, stop):
        """Protective stop at the broker. Stocks: stop order. Crypto: stop-limit."""
        self.order_seq += 1  # several stops in one run each get their own ID
        cid = f"{C.BOT_TAG}-{clean(symbol)}-stop-{self.time_key()}-{self.order_seq}"
        if S.is_crypto(symbol):
            limit = round(stop * (1 - C.CRYPTO_STOP_LIMIT_BUFFER), 2)
            order = self.client.submit_order(symbol, qty, "sell", "stop_limit", "gtc", cid,
                                             stop_price=stop, limit_price=limit)
        else:
            order = self.client.submit_order(symbol, int(qty), "sell", "stop", "gtc", cid, stop_price=stop)
        return order["id"]

    # --------------------------------------------------------- reconcile
    def reconcile(self, symbol):
        """Catch stop losses that filled at the broker since the last run,
        and make sure every open position still has a live stop order."""
        pos = self.state["positions"][symbol]
        order = self.client.get_order(pos["stop_order_id"]) if pos.get("stop_order_id") else None
        status = order["status"] if order else "missing"
        filled = float(order.get("filled_qty") or 0) if order else 0.0

        if filled > pos.get("stop_filled_booked", 0):
            new_fill = filled - pos.get("stop_filled_booked", 0)
            self.record_sell(symbol, new_fill, float(order["filled_avg_price"]), "stop loss", order)
            pos["stop_filled_booked"] = filled
        if pos["qty"] <= 1e-9:
            del self.state["positions"][symbol]
            return
        if status in OPEN_ORDER_STATUSES:
            return  # stop is live, all good

        # The stop is gone (cancelled/expired/rejected) but we still hold the position.
        held = self.client.position_qty(symbol)
        if held <= 1e-9:
            # Closed outside the bot (e.g. by hand). Book it at the last price.
            price = self.prices.get(symbol) or pos["entry_price"]
            self.error(f"{symbol}: position vanished at broker, booking exit at {price:.2f}")
            self.record_sell(symbol, pos["qty"], price, "closed outside bot", {})
            del self.state["positions"][symbol]
            return
        pos["qty"] = min(pos["qty"], held)  # never think we own more than we do
        log.warning(f"{symbol}: stop order was {status}, placing a new one")
        pos["stop_order_id"] = self.place_stop(symbol, pos["qty"], pos["stop_price"])
        pos["stop_filled_booked"] = 0

    # --------------------------------------------------------------- exit
    def close_position(self, symbol, reason):
        pos = self.state["positions"][symbol]

        # 1. Cancel the protective stop first (Alpaca locks shares behind it).
        if pos.get("stop_order_id"):
            self.client.cancel_order(pos["stop_order_id"])
            stop = self.client.wait_for_order(pos["stop_order_id"], timeout=15,
                                              final_statuses=("filled", "canceled", "expired", "rejected"))
            filled = float(stop.get("filled_qty") or 0)
            if filled > pos.get("stop_filled_booked", 0):  # it filled while we were cancelling
                self.record_sell(symbol, filled - pos.get("stop_filled_booked", 0),
                                 float(stop["filled_avg_price"]), "stop loss", stop)
                pos["stop_filled_booked"] = filled
            if stop["status"] in OPEN_ORDER_STATUSES:
                self.error(f"{symbol}: could not cancel stop, will retry next run")
                return
            pos["stop_order_id"] = None
            if pos["qty"] <= 1e-9:
                del self.state["positions"][symbol]
                return

        # 2. Never sell more than we hold (by our books AND at the broker).
        held = self.client.position_qty(symbol)
        if held < pos["qty"] - 1e-9:
            # Broker has fewer than our books say (closed outside the bot?).
            # Book the missing part at the current price so the books match reality.
            missing = pos["qty"] - max(held, 0)
            price = self.prices.get(symbol) or pos["entry_price"]
            self.error(f"{symbol}: broker holds {held}, books say {pos['qty']}; booking the difference")
            self.record_sell(symbol, missing, price, "closed outside bot", {})
        qty = pos["qty"] if S.is_crypto(symbol) else math.floor(pos["qty"])
        if qty <= 0:
            log.warning(f"{symbol}: nothing held at broker, removing from books")
            del self.state["positions"][symbol]
            return

        # 3. Market sell with a unique ID.
        cid = f"{C.BOT_TAG}-{clean(symbol)}-sell-{self.time_key()}"
        tif = "gtc" if S.is_crypto(symbol) else "day"
        try:
            order = self.client.submit_order(symbol, qty, "sell", "market", tif, cid)
            order = self.client.wait_for_order(order["id"])
        except Exception:
            # Sell failed: put the stop back so the position is never unprotected.
            pos["stop_order_id"] = self.place_stop(symbol, pos["qty"], pos["stop_price"])
            pos["stop_filled_booked"] = 0
            raise
        if order["status"] != "filled":
            self.client.cancel_order(order["id"])
            order = self.client.get_order(order["id"])
        filled = float(order.get("filled_qty") or 0)
        if filled > 0:
            self.record_sell(symbol, filled, float(order["filled_avg_price"]), reason, order)

        if pos["qty"] > 1e-9:
            self.error(f"{symbol}: only sold {filled} of {qty}; re-placing stop on the rest")
            pos["stop_order_id"] = self.place_stop(symbol, pos["qty"], pos["stop_price"])
            pos["stop_filled_booked"] = 0
        else:
            del self.state["positions"][symbol]

    def check_exit(self, symbol, market_open, minutes_to_close):
        pos = self.state["positions"][symbol]
        price = self.prices.get(symbol)

        # SPY/QQQ: flat before the close, and never carried overnight.
        if symbol in C.CLOSE_BEFORE_BELL:
            if not market_open:
                return
            opened_today = parse_time(pos["opened_at"]).astimezone(ET).date().isoformat() == self.today
            if minutes_to_close <= C.FLATTEN_MINUTES_BEFORE_CLOSE or not opened_today:
                return self.close_position(symbol, "end of day")

        if not S.is_crypto(symbol) and not market_open:
            return  # stocks can only be sold while the market is open

        # Backup stop check, in case the broker stop did not fill (e.g. a gap
        # through a crypto stop-limit).
        if price is not None and price <= pos["stop_price"]:
            return self.close_position(symbol, "stop loss (bot check)")

        df = self.bars.get(symbol)
        if df is None or df.empty:
            return
        last = df.iloc[-1]
        if symbol in C.MEAN_REVERSION_SYMBOLS:
            if self.signal_is_fresh(symbol) and S.mean_reversion_exit(last["close"], last["sma"]):
                self.close_position(symbol, "back to average")
        elif not S.trend_is_up(last["ema_fast"], last["ema_slow"]):
            self.close_position(symbol, "trend ended (20 EMA below 50 EMA)")

    # -------------------------------------------------------------- entry
    def check_entry(self, symbol, market_open, minutes_to_close, equity, cash):
        df = self.bars.get(symbol)
        if df is None or df.empty or not self.signal_is_fresh(symbol):
            return
        last = df.iloc[-1]

        if symbol in C.TREND_SYMBOLS:
            up = S.trend_is_up(last["ema_fast"], last["ema_slow"])
            if not up:
                self.state["trend_wait_for_new_cross"].pop(symbol, None)  # crossover reset
                return
            if self.state["trend_wait_for_new_cross"].get(symbol):
                return
        else:
            if symbol in C.CLOSE_BEFORE_BELL and minutes_to_close <= C.NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE:
                return
            if not S.mean_reversion_entry(last["z"], last["atr"]):
                return

        if not S.is_crypto(symbol) and not market_open:
            return
        if os.environ.get("TRADING_PAUSED", "").lower() == "true":
            log.info(f"{symbol}: signal ignored, TRADING_PAUSED is on")
            return
        ok, why = S.can_open(symbol, self.state["positions"].keys(), self.state["day"]["halted"])
        if not ok:
            log.info(f"{symbol}: signal skipped ({why})")
            return
        if self.client.open_orders(symbol):
            log.info(f"{symbol}: skipped, an order from this bot is already open")
            return

        price, atr_value = float(last["close"]), float(last["atr"])
        qty = S.position_size(symbol, equity, price, atr_value, cash)
        if qty <= 0:
            log.info(f"{symbol}: signal but size rounds to 0, skipped")
            return

        # Unique ID per symbol per signal bar -> the same signal can never be bought twice.
        cid = f"{C.BOT_TAG}-{clean(symbol)}-buy-{df.index[-1].strftime('%Y%m%dT%H%M')}"
        if self.client.get_order_by_client_id(cid):
            log.info(f"{symbol}: already acted on this bar, skipped")
            return
        self.open_position(symbol, qty, atr_value, cid)

    def open_position(self, symbol, qty, atr_value, cid):
        tif = "gtc" if S.is_crypto(symbol) else "day"
        order = self.client.submit_order(symbol, qty, "buy", "market", tif, cid)
        order = self.client.wait_for_order(order["id"])
        if order["status"] != "filled":
            self.client.cancel_order(order["id"])
            order = self.client.get_order(order["id"])
        filled = float(order.get("filled_qty") or 0)
        if filled <= 0:
            log.warning(f"{symbol}: buy order did not fill ({order['status']})")
            return
        fill_price = float(order["filled_avg_price"])
        notional = filled * fill_price

        # Alpaca takes the crypto fee out of the coins you receive. Use what is
        # really held so we never try to sell more than we have.
        held = self.client.position_qty(symbol)
        qty_held = min(filled, held) if held > 0 else filled
        if S.is_crypto(symbol) and qty_held < filled:
            fee = (filled - qty_held) * fill_price  # fee already paid in coins
            cash_flow = -notional
        else:
            fee = notional * S.fee_rate(symbol)
            cash_flow = -notional - fee
        self.record_trade(symbol, "buy", filled, fill_price, fee, cash_flow, "entry signal", order)

        stop = S.stop_price(symbol, fill_price, atr_value)
        self.state["positions"][symbol] = {
            "qty": qty_held, "entry_price": fill_price, "cost_per_unit": -cash_flow / qty_held,
            "stop_price": stop, "stop_order_id": None, "stop_filled_booked": 0,
            "strategy": S.strategy_name(symbol), "opened_at": self.now.isoformat(),
        }
        try:
            self.state["positions"][symbol]["stop_order_id"] = self.place_stop(symbol, qty_held, stop)
            log.info(f"{symbol}: stop loss placed at {stop:.2f}")
        except Exception as exc:
            # Every trade MUST have a stop. If we can't place one, get out now.
            self.error(f"{symbol}: could not place stop ({exc}); closing position")
            self.close_position(symbol, "no stop possible")

    # ---------------------------------------------------------------- run
    def run(self):
        if not os.path.exists(C.BACKTEST_REPORT):
            log.warning("No backtest report yet. Run the 'Backtest' workflow first. Not trading.")
            return 0

        account = self.client.account()  # also proves the paper keys work
        if account.get("trading_blocked"):
            log.error("Alpaca says trading is blocked on this account.")
            return 1

        self.state = ledger.load_state()
        clock = self.client.clock()
        market_open = bool(clock["is_open"])
        minutes_to_close = (parse_time(clock["next_close"]) - self.now).total_seconds() / 60

        if self.trigger == "hourly" and market_open:
            log.info("Hourly run skipped: market is open, the 15-minute run handles it.")
            return 0

        active = [s for s in C.SYMBOLS if market_open or S.is_crypto(s)]
        log.info(f"=== Run at {self.now:%Y-%m-%d %H:%M} UTC | market {'OPEN' if market_open else 'closed'}"
                 f" | trading: {', '.join(active)} ===")

        # 2. Bars and latest prices
        for symbol in active:
            try:
                self.bars[symbol] = self.load_bars(symbol, market_open)
                if not self.bars[symbol].empty:
                    self.prices[symbol] = float(self.bars[symbol]["close"].iloc[-1])
            except Exception as exc:
                self.error(f"{symbol}: could not load bars ({exc}), skipping")
        try:  # broker's live price is fresher than the last bar
            for sym_no_slash, p in self.client.positions().items():
                for symbol in C.SYMBOLS:
                    if clean(symbol) == sym_no_slash and p.get("current_price"):
                        self.prices[symbol] = float(p["current_price"])
        except Exception as exc:
            self.error(f"could not read broker positions ({exc})")

        # 3. Reconcile stops
        for symbol in list(self.state["positions"]):
            try:
                self.reconcile(symbol)
            except Exception as exc:
                self.error(f"{symbol}: reconcile failed ({exc})")

        # 4. Account value + daily loss limit
        cash = ledger.cash_balance()
        equity = cash + ledger.positions_value(self.state, self.prices)
        day = self.state["day"]
        if day["date"] != self.today:
            self.state["day"] = day = {"date": self.today, "start_equity": equity, "halted": False}
        if not day["halted"] and S.daily_loss_hit(equity, day["start_equity"]):
            day["halted"] = True
            log.warning(f"Down {C.DAILY_LOSS_LIMIT:.0%} today: no new trades until tomorrow.")

        # 5. Exits
        for symbol in list(self.state["positions"]):
            if symbol not in active:
                continue
            try:
                self.check_exit(symbol, market_open, minutes_to_close)
            except Exception as exc:
                self.error(f"{symbol}: exit check failed ({exc})")

        # 6. Entries
        for symbol in active:
            if symbol in self.state["positions"]:
                continue
            try:
                cash = ledger.cash_balance()
                equity = cash + ledger.positions_value(self.state, self.prices)
                self.check_entry(symbol, market_open, minutes_to_close, equity, cash)
            except Exception as exc:
                self.error(f"{symbol}: entry check failed ({exc})")

        # 7. Books and reports
        self.write_performance()
        ledger.save_state(self.state)
        report.write_summary(errors=self.errors)
        log.info(f"Run finished with {len(self.errors)} error(s).")
        return 1 if self.errors else 0

    def write_performance(self):
        spy = self.prices.get("SPY")
        if spy is None:
            try:
                spy = self.client.latest_price("SPY")
            except Exception as exc:
                log.warning(f"could not get SPY price ({exc})")
        if spy and not self.state.get("spy_start_price"):
            self.state["spy_start_price"] = spy
        cash = ledger.cash_balance()
        pos_value = ledger.positions_value(self.state, self.prices)
        equity = cash + pos_value
        start = self.state["day"]["start_equity"] or equity
        spy_start = self.state.get("spy_start_price")
        ledger.append_performance({
            "time_utc": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "equity": round(equity, 2), "cash": round(cash, 2), "positions_value": round(pos_value, 2),
            "open_positions": " ".join(self.state["positions"]) or "-",
            "day_pnl_pct": round((equity / start - 1) * 100, 3),
            "total_return_pct": round((equity / C.STARTING_CAPITAL - 1) * 100, 3),
            "spy_price": round(spy, 2) if spy else "",
            "spy_hold_value": round(C.STARTING_CAPITAL * spy / spy_start, 2) if spy and spy_start else "",
            "halted": self.state["day"]["halted"],
        })


def main():
    setup_logging()
    trigger = sys.argv[1] if len(sys.argv) > 1 else "schedule"
    try:
        client = AlpacaPaper(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY"))
        code = Bot(client, trigger=trigger).run()
    except Exception as exc:
        log.exception(f"Run aborted: {exc}")
        code = 1
    trim_log()
    sys.exit(code)


if __name__ == "__main__":
    main()
