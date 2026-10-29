from __future__ import annotations

import pytest

from engines.costs import GST_RATE, CostModel
from infrastructure.models import BookLevel, ContractSpec, OrderBook, Position
from layers.risk.sizing import liquidation_price, size_position


def test_from_api_converts_percent_margins(btc: ContractSpec):
    assert btc.initial_margin == pytest.approx(0.005)       # "0.5" % → 0.005
    assert btc.maintenance_margin == pytest.approx(0.0025)
    assert btc.max_leverage == pytest.approx(200)
    assert btc.contract_value == 0.001 and btc.tick_size == 0.5
    assert btc.taker_fee == 0.0005 and btc.maker_fee == 0.0002


def test_tick_rounding_directional(btc):
    assert btc.round_price(60000.3, "buy") == 60000.0
    assert btc.round_price(60000.3, "sell") == 60000.5
    assert btc.round_price(60000.3) == 60000.5 or btc.round_price(60000.3) == 60000.0
    # stops round toward safety (tighter)
    assert btc.round_stop(59000.2, "buy") == 59000.5
    assert btc.round_stop(61000.3, "sell") == 61000.0


def test_notional_and_pnl_linear(btc):
    assert btc.notional(10, 60000) == pytest.approx(600.0)       # 10 × 0.001 BTC × 60k
    assert btc.pnl("buy", 10, 60000, 61000) == pytest.approx(10.0)
    assert btc.pnl("sell", 10, 60000, 61000) == pytest.approx(-10.0)
    assert btc.contracts_for_notional(600.0, 60000) == 10


def test_position_apply_fill_add_reduce_flip(btc):
    p = Position("BTCUSD")
    p.apply_fill(btc, "buy", 10, 60000)
    p.apply_fill(btc, "buy", 10, 62000)
    assert p.size == 20 and p.entry_price == pytest.approx(61000)
    r = p.apply_fill(btc, "sell", 5, 63000)
    assert r == pytest.approx(5 * 0.001 * 2000) and p.size == 15
    r2 = p.apply_fill(btc, "sell", 25, 60000)   # close 15 and flip short 10
    assert r2 == pytest.approx(15 * 0.001 * -1000)
    assert p.size == -10 and p.entry_price == 60000


def test_fee_includes_gst_and_maker_taker(btc):
    c = CostModel()
    assert c.fee(btc, 1000, is_maker=False) == pytest.approx(1000 * 0.0005 * (1 + GST_RATE))
    assert c.fee(btc, 1000, is_maker=True) == pytest.approx(1000 * 0.0002 * 1.18)
    assert c.round_trip_fee_rate(btc) == pytest.approx(0.001 * 1.18)


def test_funding_sign():
    c = CostModel()
    assert c.funding_payment("buy", 1000, 0.0001) == pytest.approx(0.1)    # long pays
    assert c.funding_payment("sell", 1000, 0.0001) == pytest.approx(-0.1)  # short receives
    assert c.funding_payment("buy", 1000, -0.0002) == pytest.approx(-0.2)


def test_market_fill_walks_book():
    book = OrderBook("X", [BookLevel(99, 5)], [BookLevel(100, 5), BookLevel(101, 5)])
    px = CostModel().market_fill_price("buy", 100, 8, book)
    assert px == pytest.approx((5 * 100 + 3 * 101) / 8)
    # bps fallback without book
    assert CostModel().market_fill_price("sell", 100, 1, None, 10) == pytest.approx(99.9)


def test_liquidation_price():
    assert liquidation_price("buy", 100, 10, 0.005) == pytest.approx(90.5)
    assert liquidation_price("sell", 100, 10, 0.005) == pytest.approx(109.5)


def test_size_position_risk_bound(btc):
    r = size_position(btc, "buy", equity=1000, available_margin=1000, entry=60000,
                      stop=59400, leverage=5, risk_pct=0.005)
    # loss per contract ≈ 0.6 + fees (60 × 0.00118 ≈ 0.0708) → floor(5 / 0.6708) = 7
    assert r.contracts == 7 and r.binding_constraint == "risk"
    assert r.risk_amount <= 5.0


def test_size_position_margin_bound(btc):
    r = size_position(btc, "buy", equity=1000, available_margin=10, entry=60000,
                      stop=59990, leverage=2, risk_pct=0.05)
    assert r.binding_constraint == "margin"
    assert r.margin <= 10


def test_size_zero_when_below_one_contract(btc):
    r = size_position(btc, "buy", equity=10, available_margin=10, entry=60000, stop=50000,
                      leverage=5, risk_pct=0.001)
    assert r.contracts == 0
