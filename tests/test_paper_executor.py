from __future__ import annotations

import pytest

from engines.paper_executor.executor import DuplicateOrderError, PaperExecutor
from infrastructure.data.market_data import MarketDataStore
from infrastructure.models import Order, TradePlan
from tests.conftest import put_market


@pytest.fixture
def env(registry, clock, tmp_path):
    store = MarketDataStore("5m", clock=clock)
    ex = PaperExecutor(registry, store, 1000.0, slippage_bps=2, clock=clock,
                       state_path=tmp_path / "paper.json")
    put_market(store, "BTCUSD", 60000)
    return store, ex


def mk_plan(**kw):
    base = dict(symbol="BTCUSD", side="buy", contracts=10, entry_price=60000.0,
                stop_loss=59400.0, take_profit=61500.0, leverage=5, strategy_id="t")
    base.update(kw)
    return TradePlan(**base)


def test_market_entry_fills_at_ask_with_taker_fee_and_brackets(env, btc):
    store, ex = env
    o = ex.execute_plan(mk_plan())
    assert o.state == "closed" and o.filled == 10
    assert o.avg_fill_price == pytest.approx(60000.5)
    assert o.fees_paid == pytest.approx(btc.notional(10, 60000.5) * 0.0005 * 1.18)
    pos = ex.position("BTCUSD")
    assert pos.size == 10
    kinds = sorted((x.stop_price is not None, x.limit_price) for x in ex.open_orders("BTCUSD"))
    assert kinds == [(False, 61500.0), (True, None)]
    assert ex.balance == pytest.approx(1000 - o.fees_paid)


def test_duplicate_plan_rejected(env):
    _, ex = env
    p = mk_plan()
    ex.execute_plan(p)
    with pytest.raises(DuplicateOrderError):
        ex.execute_plan(p)


def test_stop_triggers_on_mark_and_cancels_tp(env):
    store, ex = env
    ex.execute_plan(mk_plan())
    put_market(store, "BTCUSD", 59500, mark=59500)
    ex.on_market("BTCUSD")
    assert ex.position("BTCUSD").size == 10              # not yet
    put_market(store, "BTCUSD", 59450, mark=59390)        # mark through stop
    ex.on_market("BTCUSD")
    assert not ex.position("BTCUSD").is_open
    assert ex.open_orders("BTCUSD") == []                 # OCO: TP cancelled
    assert ex.equity() < 1000


def test_take_profit_fills_as_maker(env, btc):
    store, ex = env
    ex.execute_plan(mk_plan(contracts=1))
    put_market(store, "BTCUSD", 61600)
    ex.on_market("BTCUSD")
    assert not ex.position("BTCUSD").is_open
    tp_fill = ex.fills[-1]
    assert tp_fill.is_maker and tp_fill.price == 61500


def test_resting_limit_partial_fills(env, registry, clock):
    store, ex = env
    put_market(store, "BTCUSD", 60000, depth=8)
    o = Order("lim1", "BTCUSD", "buy", 10, "limit_order", limit_price=59000)
    ex.submit(o)
    assert o.filled == 0 and o.state == "open"
    put_market(store, "BTCUSD", 58990, depth=4)   # 5 levels × 4 × 25 % participation = 5
    ex.on_market("BTCUSD")
    assert 0 < o.filled < 10 and o.state == "partially_filled"
    for _ in range(10):
        ex.on_market("BTCUSD")
    assert o.filled == 10 and o.state == "closed"


def test_post_only_marketable_is_cancelled(env):
    _, ex = env
    o = Order("po", "BTCUSD", "buy", 1, "limit_order", limit_price=60100, post_only=True)
    ex.submit(o)
    assert o.state == "cancelled"


def test_reduce_only_without_position_rejected(env):
    _, ex = env
    o = ex.submit(Order("ro", "BTCUSD", "sell", 1, "market_order", reduce_only=True))
    assert o.state == "rejected"


def test_modify_and_cancel(env):
    _, ex = env
    o = ex.submit(Order("l2", "BTCUSD", "buy", 3, "limit_order", limit_price=50000))
    ex.modify_order("l2", limit_price=50000.3, size=5)
    assert o.limit_price == 50000.5 and o.size == 5
    assert ex.cancel_order("l2") and not ex.cancel_order("l2")


def test_funding_charged_once_per_slot(env, clock, btc):
    store, ex = env
    ex.execute_plan(mk_plan(stop_loss=55000.0, take_profit=70000.0))
    bal = ex.balance
    clock.t += 8 * 3600     # 08:00 UTC
    put_market(store, "BTCUSD", 60000, funding=0.0001)
    ex.on_market("BTCUSD")
    ex.on_market("BTCUSD")
    paid = bal - ex.balance
    assert paid == pytest.approx(btc.notional(10, 60000) * 0.0001)


def test_liquidation_on_mark(env, registry, clock):
    store, ex = env
    ex.execute_plan(mk_plan(stop_loss=1000.0, take_profit=None, leverage=20))
    ex.cancel_all("BTCUSD")   # remove stop so liquidation is reached
    put_market(store, "BTCUSD", 56000, mark=56000)
    ex.on_market("BTCUSD")
    assert not ex.position("BTCUSD").is_open


def test_state_persists_across_restart(env, registry, clock, tmp_path):
    store, ex = env
    ex.execute_plan(mk_plan())
    ex2 = PaperExecutor(registry, store, 1000.0, clock=clock, state_path=tmp_path / "paper.json")
    assert ex2.position("BTCUSD").size == 10
    assert len(ex2.open_orders("BTCUSD")) == 2
    with pytest.raises(DuplicateOrderError):
        ex2.execute_plan(mk_plan(plan_id=ex.position("BTCUSD").plan_id))


def test_flatten_all(env):
    _, ex = env
    ex.execute_plan(mk_plan())
    assert ex.flatten_all("test") == 1
    assert not ex.positions() and not ex.open_orders()
