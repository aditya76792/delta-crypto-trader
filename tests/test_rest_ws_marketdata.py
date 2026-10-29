from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from infrastructure.data.market_data import MarketDataStore, candles_to_frame
from infrastructure.exchange.rest import AuthError, DeltaAPIError, DeltaRestClient, sign
from infrastructure.exchange.ws import DeltaWebSocket


def test_signature_matches_documented_prehash():
    sig = sign("secret", "get", "1700000000", "/v2/orders", "?product_id=1&state=open", "")
    expected = hmac.new(b"secret", b"GET1700000000/v2/orders?product_id=1&state=open",
                        hashlib.sha256).hexdigest()
    assert sig == expected


def _client(handler, **kw):
    return DeltaRestClient("https://api.test", "k", "s", transport=httpx.MockTransport(handler),
                           sleep=lambda *_: None, **kw)


def test_auth_headers_and_user_agent():
    seen = {}

    def h(req: httpx.Request):
        seen.update(req.headers)
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"success": True, "result": {"id": 1}})

    c = _client(h)
    c.place_order({"product_id": 27, "size": 1, "side": "buy", "order_type": "market_order",
                   "client_order_id": "abc"})
    assert seen["api-key"] == "k" and seen["user-agent"].startswith("delta-crypto-trader")
    exp = sign("s", "POST", seen["timestamp"], "/v2/orders", "", seen["body"])
    assert seen["signature"] == exp


def test_missing_credentials_raise():
    c = DeltaRestClient("https://api.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"success": True, "result": []})))
    with pytest.raises(AuthError):
        c.get_balances()


def test_get_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"success": True, "result": {"symbol": "BTCUSD"}})

    assert _client(h).get_ticker("BTCUSD")["symbol"] == "BTCUSD"
    assert calls["n"] == 3


def test_post_without_client_id_not_retried():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(503, json={"success": False, "error": {"code": "unavailable"}})

    with pytest.raises(DeltaAPIError):
        _client(h).place_order({"product_id": 1, "size": 1, "side": "buy",
                                "order_type": "market_order"})
    assert calls["n"] == 1


def test_429_honours_reset_header_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "1"})
        return httpx.Response(200, json={"success": True, "result": []})

    c = _client(h)
    c._limiter.acquire = lambda w, s=None: None   # don't actually sleep for the block
    assert c.get_tickers() == []
    assert calls["n"] == 2


def test_business_error_not_retried_and_mapped():
    def h(req):
        return httpx.Response(400, json={"success": False,
                                         "error": {"code": "insufficient_margin",
                                                   "context": {"available": "1"}}})

    with pytest.raises(DeltaAPIError) as e:
        _client(h).place_order({"client_order_id": "x", "size": 1})
    assert e.value.code == "insufficient_margin" and not e.value.retryable


def test_ip_not_whitelisted_is_auth_error():
    def h(req):
        return httpx.Response(401, json={"success": False,
                                         "error": {"code": "ip_not_whitelisted_for_api_key"}})

    with pytest.raises(AuthError):
        _client(h).get_balances()


# ── WebSocket ────────────────────────────────────────────────────────────────

def test_ws_auth_and_subscribe_messages():
    ws = DeltaWebSocket("wss://x", "key", "sec")
    m = ws.auth_message(ts=1700000000)
    assert m["type"] == "auth"
    assert m["payload"]["signature"] == sign("sec", "GET", "1700000000", "/live")
    ws.subscribe("v2/ticker", ["BTCUSD"])
    ws.subscribe_private("orders")
    sub = ws.subscribe_message()["payload"]["channels"]
    assert {"name": "v2/ticker", "symbols": ["BTCUSD"]} in sub
    assert {"name": "orders", "symbols": ["all"]} in sub


class _FakeConn:
    def __init__(self, msgs, fail=False):
        self.msgs = list(msgs)
        self.sent = []
        self.fail = fail

    async def send(self, m):
        self.sent.append(json.loads(m))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.msgs:
            return self.msgs.pop(0)
        if self.fail:
            raise ConnectionError("dropped")
        raise StopAsyncIteration

    async def close(self):
        pass


async def test_ws_reconnects_resubscribes_and_dispatches():
    conns = [_FakeConn([json.dumps({"type": "v2/ticker", "symbol": "BTCUSD"})], fail=True),
             _FakeConn([json.dumps({"type": "heartbeat"}),
                        json.dumps({"type": "v2/ticker", "symbol": "ETHUSD"})])]
    opened = []

    async def connect(url):
        c = conns[len(opened)]
        opened.append(c)
        return c

    ws = DeltaWebSocket("wss://x", connect=connect, max_backoff_s=0.01)
    ws.subscribe("v2/ticker", ["BTCUSD"])
    got, reconnects = [], []
    ws.on_message(lambda m: got.append(m["symbol"]))
    ws.on_reconnect(lambda: reconnects.append(1))
    task = asyncio.create_task(ws.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(got) >= 2:
            break
    await ws.stop()
    task.cancel()
    assert got == ["BTCUSD", "ETHUSD"]
    assert reconnects == [1]
    assert any(m.get("type") == "subscribe" for m in conns[1].sent)   # re-subscribed
    assert conns[1].sent[0] == {"type": "enable_heartbeat"}


# ── market data store ────────────────────────────────────────────────────────

def test_ticker_parsing_funding_percent_to_fraction(clock):
    st = MarketDataStore("5m", clock=clock)
    st.handle({"type": "v2/ticker", "symbol": "BTCUSD", "close": "60000", "mark_price": "60010",
               "spot_price": "60005", "funding_rate": "0.01",
               "quotes": {"best_bid": "59999.5", "best_ask": "60000.5"}})
    t = st.tickers["BTCUSD"]
    assert t.mark == 60010 and t.index == 60005 and t.funding_rate == pytest.approx(0.0001)
    assert t.spread_pct == pytest.approx(1 / 60000)
    assert not st.is_stale("BTCUSD", 15)
    clock.t += 30
    assert st.is_stale("BTCUSD", 15)
    assert st.is_stale("ETHUSD", 15)   # never seen = stale


def test_candle_closes_on_new_start_time(clock):
    st = MarketDataStore("1m", clock=clock)
    base = 1_700_000_040_000_000
    m = {"type": "candlestick_1m", "symbol": "BTCUSD", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 10}
    assert st.handle({**m, "candle_start_time": base}) == []
    assert st.handle({**m, "close": 1.8, "candle_start_time": base}) == []
    closed = st.handle({**m, "candle_start_time": base + 60_000_000})
    assert len(closed) == 1 and closed[0][1].close == 1.8
    assert len(st.bars("BTCUSD")) == 1


def test_candles_to_frame_and_seed_drops_forming(clock):
    rows = [{"time": int(clock.t) - 600 + i * 300, "open": 1, "high": 1, "low": 1, "close": 1,
             "volume": 1} for i in range(3)]
    df = candles_to_frame(rows)
    st = MarketDataStore("5m", clock=clock)
    st.seed_candles("BTCUSD", df)
    assert len(st.bars("BTCUSD")) == 2   # last one (opens at now) is still forming
