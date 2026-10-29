"""
Position sizing and liquidation math for linear crypto derivatives.

Sizing takes the MIN of:
  1. risk budget:   equity × risk_pct / (stop distance + round-trip fees) per contract
  2. margin budget: available margin × leverage / notional per contract
  3. exposure caps: per-symbol and total notional caps (multiples of equity)
  4. exchange position_size_limit
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engines.costs import CostModel
from infrastructure.models import ContractSpec, Side


def liquidation_price(side: Side, entry: float, leverage: float, mmr: float) -> float:
    """
    Approximate isolated-margin liquidation price for a linear contract.
    Long:  entry × (1 − 1/L + mmr)    Short: entry × (1 + 1/L − mmr)
    Live mode prefers the exchange-reported ``liquidation_price``.
    """
    im = 1.0 / max(leverage, 1e-9)
    if side == "buy":
        return entry * (1 - im + mmr)
    return entry * (1 + im - mmr)


def required_margin(spec: ContractSpec, contracts: int, price: float, leverage: float) -> float:
    lev = min(leverage, spec.max_leverage)
    return spec.notional(contracts, price) / lev


@dataclass
class SizeResult:
    contracts: int
    binding_constraint: str
    risk_amount: float
    notional: float
    margin: float
    liq_price: float


def size_position(spec: ContractSpec, side: Side, equity: float, available_margin: float,
                  entry: float, stop: float, leverage: float, risk_pct: float,
                  symbol_exposure: float = 0.0, total_exposure: float = 0.0,
                  max_symbol_exposure_x: float = 2.0, max_total_exposure_x: float = 3.0,
                  costs: CostModel | None = None) -> SizeResult:
    costs = costs or CostModel()
    leverage = max(1.0, min(leverage, spec.max_leverage))
    per_contract_notional = spec.notional(1, entry)
    stop_dist = abs(entry - stop)
    fee_per_contract = per_contract_notional * costs.round_trip_fee_rate(spec)
    loss_per_contract = spec.contract_value * stop_dist + fee_per_contract
    caps: dict[str, float] = {}
    if loss_per_contract > 0:
        caps["risk"] = equity * risk_pct / loss_per_contract
    caps["margin"] = (available_margin * leverage * 0.95) / per_contract_notional  # 5 % buffer
    caps["symbol_exposure"] = max(0.0, equity * max_symbol_exposure_x - symbol_exposure) \
        / per_contract_notional
    caps["total_exposure"] = max(0.0, equity * max_total_exposure_x - total_exposure) \
        / per_contract_notional
    caps["exchange_limit"] = float(spec.position_size_limit)
    binding = min(caps, key=lambda k: caps[k])
    n = max(0, int(math.floor(caps[binding] + 1e-9)))
    if n < spec.min_size:
        n = 0
    return SizeResult(
        contracts=n,
        binding_constraint=binding,
        risk_amount=n * loss_per_contract,
        notional=spec.notional(n, entry),
        margin=required_margin(spec, n, entry, leverage) if n else 0.0,
        liq_price=liquidation_price(side, entry, leverage, spec.maintenance_margin),
    )
