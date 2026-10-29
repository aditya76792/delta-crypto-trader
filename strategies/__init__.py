"""Strategy registry."""

from __future__ import annotations

from typing import Any

from strategies.base import BaseStrategy, TradeSignal
from strategies.bb_squeeze_breakout import BBSqueezeBreakout
from strategies.ema_trend_scalp import EMATrendScalp
from strategies.liquidity_sweep import LiquiditySweep
from strategies.vwap_reversion import VWAPReversion

REGISTRY: dict[str, type[BaseStrategy]] = {
    cls.strategy_id: cls
    for cls in (EMATrendScalp, VWAPReversion, BBSqueezeBreakout, LiquiditySweep)
}


def build_strategy(name: str, params: dict[str, Any] | None = None) -> BaseStrategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; choose from {sorted(REGISTRY)}")
    return REGISTRY[name](**(params or {}))


__all__ = ["BaseStrategy", "TradeSignal", "REGISTRY", "build_strategy"]
