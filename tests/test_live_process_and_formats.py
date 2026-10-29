"""Official-docs message formats, REST candle fallback, live approval process,
per-entry confirmation and the validation scenarios."""

from __future__ import annotations

import pytest

from infrastructure.config.settings import Settings
from infrastructure.data.market_data import MarketDataStore


def test_compact_public_formats(clock):
    st = MarketDataStore("1m", clock=clock)
    # ticker (public endpoint sample shape)
    st.handle({"type": "ticker", "sy": "BTCUSD", "sp": "72154.6", "ts": int(clock.t * 1e6),
               "d": [{"s": "BTCUSD", "m": "72124.5", "q": ["72101", "822", "72100", "2123", None],
                      "ohlc": [71009.0, 73135.5, 70495.0, 72123.5], "oi": ["537395", "4457821.69"],
                      "pb": ["68495.1", "75705.1"], "to": [1.1e9, 1.15e9], "m24hc": "1.59"}]})
    t = st.tickers["BTCUSD"]
    assert t.last == 72123.5 and t.mark == 72124.5 and t.index == 72154.6
    assert t.best_ask == 72101 and t.best_bid == 72100 and t.open_interest == 537395
    assert t.turnover_usd_24h == 1.15e9 and t.band_high == 75705.1
    # funding (percent → fraction) keeps across later tickers
    st.handle({"type": "funding_rate", "sy": "BTCUSD", "fr": 0.01, "fi": 28800,
               "nfr": 1775836800000000, "ts": 1})
    assert t.funding_rate == pytest.approx(0.0001) and t.funding_interval_s == 28800
    st.handle({"type": "ticker", "sy": "BTCUSD", "d": [{"s": "BTCUSD", "m": "1", "ohlc": [1, 1, 1, 1]}]})
    assert st.tickers["BTCUSD"].funding_rate == pytest.approx(0.0001)
    # ob_l2 arrays
    st.handle({"type": "ob_l2", "sy": "BTCUSD", "a": [["68525.0", "3313"], ["68525.5", "3009"]],
               "b": [["68524.0", "2452"]], "ts": 1})
    assert st.books["BTCUSD"].best_ask == 68525.0 and st.books["BTCUSD"].best_bid == 68524.0
    # mark price
    st.handle({"type": "mark_price", "sy": "MARK:BTCUSD", "p": "72000.5", "ts": 1})
    assert st.tickers["BTCUSD"].mark == 72000.5
    # compact candles: start time is floored from ts
    base = (int(clock.t) // 60) * 60 - 120
    m = {"type": "candlestick_1m", "sy": "BTCUSD", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}
    st.handle({**m, "ts": int((base + 30) * 1e6)})
    closed = st.handle({**m, "c": 1.7, "ts": int((base + 75) * 1e6)})
    assert len(closed) == 1 and closed[0][1].ts.timestamp() == base
    # mark candles are ignored for bar closing
    assert st.handle({**m, "sy": "MARK:BTCUSD", "ts": int((base + 200) * 1e6)}) == []


def test_rest_candle_fallback_closes_once(clock):
    st = MarketDataStore("1m", clock=clock)
    now = int(clock.t) // 60 * 60
    rows = [{"time": now - 120, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"time": now - 60, "open": 1, "high": 1, "low": 1, "close": 2, "volume": 1},
            {"time": now, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1}]   # forming
    got = st.close_from_rest("BTCUSD", rows)
    assert [c.close for _, c in got] == [1, 2]
    assert st.close_from_rest("BTCUSD", rows) == []                     # idempotent
    # a late WS update for an already-closed bar is ignored
    assert st.handle({"type": "candlestick_1m", "symbol": "BTCUSD", "candle_start_time":
                      (now - 60) * 1_000_000, "open": 9, "high": 9, "low": 9, "close": 9}) == []


def test_live_ws_private_payloads(tmp_path, registry):
    from tests.test_live_executor import FakeRest, build, mk_plan
    registry.source = "api"
    rest = FakeRest()
    rest.place_order = lambda body: {"id": 77, "state": "open", "size": 3, "unfilled_size": 3}
    ex = build(tmp_path, registry, rest)
    p = mk_plan(order_type="limit_order", limit_price=59990.0)
    ex.execute_plan(p)
    # orders channel uses order_id + symbol; snapshot wrapper on subscribe
    ex.handle_ws({"type": "orders", "action": "snapshot", "result": [
        {"id": 77, "client_order_id": p.client_order_id, "product_symbol": "BTCUSD",
         "size": 3, "unfilled_size": 3, "state": "open"}]})
    ex.handle_ws({"type": "orders", "action": "update", "reason": "fill", "symbol": "BTCUSD",
                  "order_id": 77, "client_order_id": p.client_order_id, "size": 3,
                  "unfilled_size": 0, "state": "closed", "average_fill_price": "59990"})
    o = ex._orders[p.client_order_id]
    assert o.exchange_id == "77" and o.filled == 3 and o.state == "closed"
    ex.handle_ws({"type": "positions", "symbol": "BTCUSD", "size": 3, "entry_price": "59990",
                  "liquidation_price": "48000", "realized_pnl": "0"})
    ex.handle_ws({"type": "positions", "symbol": "BTCUSD", "size": 0, "entry_price": "0",
                  "realized_pnl": "-1.5"})
    assert not ex.position("BTCUSD").is_open


def test_approve_live_requires_tty_readiness_and_phrase(tmp_path, monkeypatch):
    from layers.automation import live_process as LP
    s = Settings(symbols=["BTCUSD"], data_dir=tmp_path)
    with pytest.raises(PermissionError, match="terminal"):
        LP.approve_live(s, prompt=lambda _: LP.PHRASE, tty=False)
    with pytest.raises(PermissionError, match="not ready"):
        LP.approve_live(s, prompt=lambda _: LP.PHRASE, tty=True)
    LP.run_validation(write=True)
    import time
    LP.write_paper_report(time.time() - 5 * 3600, 0, 0, [], 1000, 1001)
    with pytest.raises(PermissionError, match="did not match"):
        LP.approve_live(s, prompt=lambda _: "yes", tty=True)
    LP.approve_live(s, prompt=lambda _: LP.PHRASE, tty=True)
    assert LP.approval_status(s)[0]
    s.risk.max_daily_loss_pct = 0.05
    assert not LP.approval_status(s)[0]
    assert LP.revoke_live()


def test_validation_passes():
    from layers.automation.live_process import run_validation
    doc = run_validation(write=False)
    assert doc["passed"], [x for x in doc["scenarios"] if not x["passed"]]
    assert {x["name"] for x in doc["scenarios"]} >= {
        "target_hit", "stop_hit", "gap_through_stop", "time_stop", "stale_data_blocks_entry",
        "kill_switch_blocks_entry", "daily_loss_halts_and_flattens", "duplicate_plan_refused"}


def test_live_entry_waits_for_typed_confirmation(tmp_path, registry):
    from engines.paper_executor.executor import PaperExecutor
    from infrastructure.data.history import synthetic_candles
    from infrastructure.workspace import path, read_json, write_json
    from layers.automation.replay import ReplayClock, replay_messages
    from layers.automation.trader import Trader
    from layers.risk.state import RiskState
    from strategies import build_strategy
    from strategies.base import TradeSignal
    clock = ReplayClock()
    s = Settings(symbols=["BTCUSD"], data_dir=tmp_path)
    store = MarketDataStore("5m", clock=clock)
    ex = PaperExecutor(registry, store, 1000, clock=clock)
    tr = Trader(s, registry, store, ex, build_strategy("ema_trend_scalp"), RiskState(tmp_path),
                clock=clock)
    for m in replay_messages(synthetic_candles(250), "BTCUSD", "5m", clock):
        tr.handle_message(m)
    tr.confirm_entries = True
    px = store.tickers["BTCUSD"].last
    tr.try_enter("BTCUSD", TradeSignal("BUY", 1, px * 0.99, px * 1.03), store.bars("BTCUSD"))
    assert len(tr.pending) == 1 and not ex.position("BTCUSD").is_open
    pid = next(iter(tr.pending))
    assert read_json(path(f"live/pending/{pid}.json"))["symbol"] == "BTCUSD"
    tr.on_tick()
    assert not ex.position("BTCUSD").is_open                       # still waiting
    write_json(path(f"live/confirmed/{pid}.json"), {"answer": "yes"})
    tr.on_tick()
    assert ex.position("BTCUSD").is_open and not tr.pending
    # an unanswered request expires
    ex.flatten_all("t")
    tr.try_enter("BTCUSD", TradeSignal("BUY", 1, px * 0.99, px * 1.03), store.bars("BTCUSD"))
    clock.t += 121
    tr.on_tick()
    assert not tr.pending and tr.decisions[-1]["kind"] in ("expired", "veto")


def test_alert_types():
    from datetime import datetime, timezone

    from desk.alerts import evaluate
    q = {"close": "100", "mark_price": "100.6", "funding_rate": "0.08", "mark_change_24h": "-6",
         "quotes": {"best_bid": "99", "best_ask": "101"}, "oi_change_usd_6h": "-5000000"}
    now = datetime(2026, 1, 1, 7, 50, tzinfo=timezone.utc)
    assert evaluate({"type": "stop", "symbol": "X", "side": "buy", "price": 101}, q, now)
    assert not evaluate({"type": "stop", "symbol": "X", "side": "sell", "price": 101}, q, now)
    assert evaluate({"type": "pct_move", "symbol": "X", "pct": 5, "direction": "down"}, q, now)
    assert evaluate({"type": "funding", "symbol": "X", "rate": 0.0005}, q, now)
    assert evaluate({"type": "funding_soon", "symbol": "X", "minutes": 15}, q, now,
                    {"X": {"size": 3}})                              # 08:00 UTC funding
    assert evaluate({"type": "liq_distance", "symbol": "X", "pct": 5}, q, now,
                    {"X": {"size": 3, "liq": 97.0}})
    assert evaluate({"type": "mark_divergence", "symbol": "X", "pct": 0.5}, q, now)
    assert evaluate({"type": "spread", "symbol": "X", "pct": 1.0}, q, now)
    assert evaluate({"type": "oi_change", "symbol": "X", "usd": 1e6}, q, now)
    assert evaluate({"type": "time", "at": "07:30"}, None, now)
