"""
Signal → TradePlan (shared by backtester, paper and live so behaviour is identical).

Applies exchange rules (tick rounding, whole contracts, leverage caps) and the
position sizer. Returns None when the signal cannot be turned into a valid order.
"""

from __future__ import annotations

from dataclasses import dataclass

from engines.costs import CostModel
from infrastructure.config.settings import RiskSettings
from infrastructure.models import ContractSpec, TradePlan
from layers.risk.sizing import SizeResult, size_position
from strategies.base import TradeSignal


@dataclass
class PlanResult:
    plan: TradePlan | None
    sizing: SizeResult | None
    reason: str = ""


def build_plan(signal: TradeSignal, spec: ContractSpec, ref_price: float, equity: float,
               available_margin: float, leverage: float, risk: RiskSettings,
               strategy_id: str, symbol_exposure: float = 0.0, total_exposure: float = 0.0,
               costs: CostModel | None = None,
               min_stop_fee_multiple: float = 2.0) -> PlanResult:
    side = signal.side
    if side is None or signal.suggested_sl is None:
        return PlanResult(None, None, "signal_not_actionable")
    lev = max(1.0, min(leverage, risk.max_leverage, spec.max_leverage))
    stop = spec.round_stop(signal.suggested_sl, side)
    tp = None
    if signal.suggested_target is not None:
        tp = spec.round_price(signal.suggested_target)
    if (side == "buy" and stop >= ref_price) or (side == "sell" and stop <= ref_price):
        return PlanResult(None, None, "stop_on_wrong_side")
    # Crypto-specific edge check: a stop tighter than a few round-trip fees is pure
    # fee bleed (taker+taker+18 % GST ≈ 0.12 % on Delta India futures).
    costs = costs or CostModel()
    min_dist = min_stop_fee_multiple * costs.round_trip_fee_rate(spec) * ref_price
    if abs(ref_price - stop) < min_dist:
        return PlanResult(None, None, "stop_too_tight_vs_fees")
    sizing = size_position(spec, side, equity, available_margin, ref_price, stop, lev,
                           risk.risk_per_trade_pct, symbol_exposure, total_exposure,
                           risk.max_symbol_exposure_x, risk.max_total_exposure_x, costs)
    if sizing.contracts <= 0:
        return PlanResult(None, sizing, f"size_zero:{sizing.binding_constraint}")
    plan = TradePlan(symbol=spec.symbol, side=side, contracts=sizing.contracts,
                     entry_price=ref_price, stop_loss=stop, take_profit=tp, leverage=lev,
                     strategy_id=strategy_id,
                     metadata={**signal.metadata, "binding": sizing.binding_constraint,
                               "est_liq": round(sizing.liq_price, 4)})
    return PlanResult(plan, sizing)
