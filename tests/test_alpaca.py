"""The HTTP client, with fake responses shaped like Alpaca's API."""

import json
from datetime import datetime, timezone

from bot.alpaca import AlpacaPaper


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_bars_follow_pagination():
    page1 = {"bars": {"SPY": [{"t": "2026-11-18T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
             "next_page_token": "abc"}
    page2 = {"bars": {"SPY": [{"t": "2026-11-18T14:45:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 5}]},
             "next_page_token": None}
    session = FakeSession([FakeResponse(200, page1), FakeResponse(200, page2)])
    client = AlpacaPaper("PKTEST", "secret", session=session)
    df = client.bars("SPY", "15Min", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert list(df["close"]) == [1.5, 1.8]
    assert session.calls[1][2]["params"]["page_token"] == "abc"
    assert session.calls[0][2]["params"]["adjustment"] == "all"
    assert session.calls[0][1].startswith("https://data.alpaca.markets/v2/stocks/bars")


def test_crypto_bars_endpoint():
    body = {"bars": {"BTC/USD": [{"t": "2026-11-18T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]}}
    session = FakeSession([FakeResponse(200, body)])
    df = AlpacaPaper("PKTEST", "s", session=session).bars("BTC/USD", "15Min", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert len(df) == 1
    assert "/v1beta3/crypto/us/bars" in session.calls[0][1]


def test_orders_go_to_paper_and_duplicate_id_returns_existing():
    existing = {"id": "o1", "client_order_id": "mr2-SPY-buy-x", "status": "filled"}
    session = FakeSession([
        FakeResponse(422, {"message": "client_order_id must be unique"}),
        FakeResponse(200, existing),
    ])
    client = AlpacaPaper("PKTEST", "s", session=session)
    order = client.submit_order("SPY", 3, "buy", "market", "day", "mr2-SPY-buy-x")
    assert order["id"] == "o1"
    method, url, kwargs = session.calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/orders"
    assert kwargs["json"]["qty"] == "3"
