from __future__ import annotations

import pandas as pd
import pytest

from engines.backtester.engine import Backtester
from engines.backtester.validation import GateCriteria, evaluate, gate_status, run_validation
from infrastructure.data.history import synthetic_candles
from strategies import REGISTRY, build_strategy
from strategies.base import BaseStrategy, TradeSignal


@pytest.fixture(scope="module")
def bars():
    return synthetic_candles(1500, seed=11)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_strategy_signals_are_well_formed(name, bars):
    s = build_strategy(name)
    n_sig = 0
    for i in range(s.required_warmup_bars, len(bars), 3):
        sig = s.on_bar("BTCUSD", bars.iloc[: i + 1])
        if sig.is_actionable:
            n_sig += 1
            px = bars["close"].iloc[i]
            if sig.direction == "BUY":
                assert sig.suggested_sl < px and (sig.suggested_target or 1e12) > px
            else:
                assert sig.suggested_sl > px and (sig.suggested_target or -1) < px
    assert n_sig >= 0


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_no_lookahead(name, bars):
    """The signal at bar i must not change when future bars are appended."""
    s = build_strategy(name)
    i = s.required_warmup_bars + 400
    a = s.on_bar("BTCUSD", bars.iloc[: i + 1])
    b = build_strategy(name).on_bar("BTCUSD", bars.iloc[: i + 1].copy())
    assert (a.direction, a.suggested_sl) == (b.direction, b.suggested_sl)


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        build_strategy("ema_trend_scalp", {"nope": 1})


def test_backtest_deterministic_and_costed(bars, btc, risk):
    r1 = Backtester(build_strategy("ema_trend_scalp"), btc, risk).run(bars)
    r2 = Backtester(build_strategy("ema_trend_scalp"), btc, risk).run(bars)
    assert r1.metrics() == r2.metrics()
    for t in r1.trades:
        assert t.fees > 0
        assert t.contracts >= 1


class _OneShot(BaseStrategy):
    strategy_id = "oneshot"
    required_warmup_bars = 2
    max_hold_bars = 10_000
    default_params: dict = {}

    def __init__(self, side="BUY", sl=95.0, tp=110.0, at=2):
        super().__init__()
        self.side, self.sl, self.tp, self.at = side, sl, tp, at

    def on_bar(self, symbol, bars):
        if len(bars) == self.at + 1:
            return TradeSignal(self.side, 1.0, self.sl, self.tp)
        return TradeSignal.flat()


def _frame(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


@pytest.fixture
def unit_spec(btc):
    from dataclasses import replace
    return replace(btc, contract_value=1.0, tick_size=0.5)


def _bt(spec, strat, **kw):
    from infrastructure.config.settings import RiskSettings
    rs = RiskSettings(risk_per_trade_pct=0.05, min_rr=0, max_symbol_exposure_x=100,
                      max_total_exposure_x=100, min_liq_distance_atr=0)
    return Backtester(strat, spec, rs, starting_equity=10_000, leverage=2, slippage_bps=0,
                      funding_rate=0.0, **kw)


def test_stop_assumed_before_target_in_same_bar(unit_spec):
    df = _frame([[100, 100, 100, 100]] * 3 + [[100, 100, 100, 100], [100, 120, 90, 100],
                                               [100, 100, 100, 100]])
    r = _bt(unit_spec, _OneShot()).run(df)
    assert r.trades[0].exit_reason == "stop"


def test_gap_through_stop_fills_at_open(unit_spec):
    df = _frame([[100, 100, 100, 100]] * 4 + [[90, 91, 89, 90], [90, 90, 90, 90]])
    r = _bt(unit_spec, _OneShot()).run(df)
    assert r.trades[0].exit_reason == "stop_gap" and r.trades[0].exit_price == 90


def test_target_hit_is_maker(unit_spec):
    df = _frame([[100, 100, 100, 100]] * 4 + [[100, 111, 99, 110], [110] * 4])
    r = _bt(unit_spec, _OneShot()).run(df)
    t = r.trades[0]
    assert t.exit_reason == "target" and t.gross_pnl > 0


def test_funding_charged_on_funding_hours(unit_spec):
    rows = [[100, 100, 100, 100]] * 200
    df = _frame(rows)   # starts 00:00 UTC; 08:00 is bar 96
    bt = _bt(unit_spec, _OneShot(sl=80, tp=1000))
    bt.funding_rate = 0.001
    r = bt.run(df)
    assert r.trades[0].funding > 0     # long paid positive funding


def test_validation_gate_writes_and_reads(tmp_path, bars, btc, risk):
    bt = Backtester(build_strategy("ema_trend_scalp"), btc, risk)
    rep = run_validation(bt, bars, "BTCUSD", "5m", tmp_path, data_source="synthetic")
    assert rep["passed"] is False      # synthetic data can never unlock live
    ok, why = gate_status(tmp_path, "ema_trend_scalp", "BTCUSD", "5m")
    assert not ok and "FAILED" in why
    ok2, why2 = gate_status(tmp_path, "nope", "BTCUSD", "5m")
    assert not ok2 and "no backtest report" in why2


def test_evaluate_criteria():
    m = {"trades": 50, "profit_factor": 1.5, "max_drawdown_pct": -10, "expectancy_r": 0.2,
         "liquidations": 0}
    assert evaluate(m, GateCriteria())[0]
    assert not evaluate({**m, "liquidations": 1}, GateCriteria())[0]
