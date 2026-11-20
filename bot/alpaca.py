"""Tiny Alpaca REST client. PAPER TRADING ONLY.

Uses plain HTTP requests (no SDK) so it is easy to see exactly what is sent.
"""

import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from bot import config as C

log = logging.getLogger("bot")

OPEN_ORDER_STATUSES = {
    "new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
    "held", "pending_replace", "pending_cancel", "calculated", "stopped", "suspended",
}


class LiveTradingRefused(RuntimeError):
    """Raised if anything suggests we are pointed at a real-money account."""


class AlpacaError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Alpaca HTTP {status}: {message}")
        self.status = status


def fmt_qty(qty) -> str:
    """'5' for shares, '0.0312' for crypto - no float noise like 0.030000000000000002."""
    return f"{float(qty):.9f}".rstrip("0").rstrip(".")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaPaper:
    def __init__(self, key_id: str, secret_key: str, session=None):
        if not key_id or not secret_key:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (set them as GitHub Secrets).")
        # Safety check 1: the URL is hardcoded to paper and must stay that way.
        if C.PAPER_TRADING_URL != "https://paper-api.alpaca.markets":
            raise LiveTradingRefused("Trading URL is not the Alpaca paper endpoint. Refusing to run.")
        # Safety check 2: Alpaca live keys start with "AK", paper keys with "PK".
        if key_id.strip().upper().startswith("AK"):
            raise LiveTradingRefused("This looks like a LIVE Alpaca key (starts with AK). Use your PAPER key.")
        self.base = C.PAPER_TRADING_URL
        self.session = session or requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key_id.strip(), "APCA-API-SECRET-KEY": secret_key.strip()})

    # ------------------------------------------------------------------ http
    def _request(self, method, url, **kwargs):
        """Send a request, retrying a few times on rate limits / server errors.

        Retrying an order is safe because every order has a unique
        client_order_id: Alpaca rejects a second order with the same ID.
        """
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise AlpacaError("network", str(exc)) from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise AlpacaError(resp.status_code, resp.text[:300])
            return resp.json() if resp.text else None
        raise AlpacaError("retry", "gave up")

    def _trading(self, method, path, **kwargs):
        return self._request(method, f"{self.base}{path}", **kwargs)

    # --------------------------------------------------------------- account
    def account(self):
        return self._trading("GET", "/v2/account")

    def clock(self):
        return self._trading("GET", "/v2/clock")

    def positions(self) -> dict:
        """All broker positions keyed by symbol WITHOUT the slash (BTC/USD -> BTCUSD)."""
        return {p["symbol"]: p for p in self._trading("GET", "/v2/positions")}

    def position_qty(self, symbol: str) -> float:
        """Shares/coins actually held at the broker (0 if none)."""
        try:
            pos = self._trading("GET", f"/v2/positions/{symbol.replace('/', '')}")
        except AlpacaError as exc:
            if exc.status == 404:
                return 0.0
            raise
        return float(pos["qty"])

    # ---------------------------------------------------------------- orders
    def get_order(self, order_id: str):
        return self._trading("GET", f"/v2/orders/{order_id}")

    def get_order_by_client_id(self, client_order_id: str):
        try:
            return self._trading("GET", "/v2/orders:by_client_order_id",
                                 params={"client_order_id": client_order_id})
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise

    def open_orders(self, symbol: str):
        """Open orders for one symbol that THIS bot placed (tagged client_order_id)."""
        orders = self._trading("GET", "/v2/orders", params={"status": "open", "limit": 500})
        want = symbol.replace("/", "")
        return [o for o in orders
                if o["symbol"].replace("/", "") == want
                and (o.get("client_order_id") or "").startswith(C.BOT_TAG + "-")]

    def submit_order(self, symbol, qty, side, order_type, time_in_force, client_order_id,
                     stop_price=None, limit_price=None):
        body = {
            "symbol": symbol,
            "qty": fmt_qty(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        }
        if stop_price is not None:
            body["stop_price"] = str(stop_price)
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        try:
            return self._trading("POST", "/v2/orders", json=body)
        except AlpacaError as exc:
            # A retry after a network blip can hit "client_order_id must be unique".
            # That means the first attempt worked, so return that order.
            if exc.status == 422 and "client_order_id" in str(exc):
                existing = self.get_order_by_client_id(client_order_id)
                if existing:
                    return existing
            raise

    def cancel_order(self, order_id: str):
        try:
            self._trading("DELETE", f"/v2/orders/{order_id}")
        except AlpacaError as exc:
            if exc.status not in (404, 422):  # 422 = already filled/cancelled
                raise

    def wait_for_order(self, order_id: str, timeout: float = 30, final_statuses=("filled",)):
        """Poll an order until it reaches a final state or we time out."""
        deadline = time.time() + timeout
        order = self.get_order(order_id)
        while order["status"] not in final_statuses and order["status"] in OPEN_ORDER_STATUSES:
            if time.time() > deadline:
                break
            time.sleep(1)
            order = self.get_order(order_id)
        return order

    # ------------------------------------------------------------ market data
    def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None,
             feed: str = C.LIVE_STOCK_FEED) -> pd.DataFrame:
        """Historical bars, following pagination. Index = bar START time (UTC)."""
        params = {"symbols": symbol, "timeframe": timeframe, "start": iso(start), "limit": 10000}
        if end is not None:
            params["end"] = iso(end)
        if symbol in C.CRYPTO_SYMBOLS:
            url = f"{C.DATA_URL}/v1beta3/crypto/us/bars"
        else:
            url = f"{C.DATA_URL}/v2/stocks/bars"
            params.update({"feed": feed, "adjustment": "all"})  # adjust for splits & dividends

        rows = []
        while True:
            data = self._request("GET", url, params=params) or {}
            rows += (data.get("bars") or {}).get(symbol, [])
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows).rename(columns={"t": "time", "o": "open", "h": "high",
                                                "l": "low", "c": "close", "v": "volume"})
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)
        return df[~df.index.duplicated()].sort_index()

    def latest_price(self, symbol: str) -> float | None:
        if symbol in C.CRYPTO_SYMBOLS:
            data = self._request("GET", f"{C.DATA_URL}/v1beta3/crypto/us/latest/trades",
                                 params={"symbols": symbol})
            trade = (data.get("trades") or {}).get(symbol)
        else:
            data = self._request("GET", f"{C.DATA_URL}/v2/stocks/{symbol}/trades/latest",
                                 params={"feed": C.LIVE_STOCK_FEED})
            trade = data.get("trade")
        return float(trade["p"]) if trade else None
