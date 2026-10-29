"""
Abstract base class for all strategies (interface preserved from the equity system).

Strategies are pure functions of bar history — no I/O, no order handling. They
return a TradeSignal with direction, a suggested stop and target. Sizing, leverage,
funding and liquidation checks live in the risk layer, not here.

Crypto changes vs. the equity base:
  * shorts are first-class (perpetuals), so every strategy emits BUY and SELL
  * ``product_type`` (CNC/MIS) is replaced by ``holding`` ("scalp" / "intraday"),
    with ``max_hold_bars`` as a time stop because there is no end-of-day square-off
  * parameters are configurable via ``params`` (config.yaml → strategy_params)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class TradeSignal:
    direction: str                      # "BUY", "SELL", "FLAT", or "EXIT"
    strength: float = 0.0               # 0.0–1.0
    suggested_sl: float | None = None
    suggested_target: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("BUY", "SELL")

    @property
    def side(self) -> str | None:
        return {"BUY": "buy", "SELL": "sell"}.get(self.direction)

    @classmethod
    def flat(cls) -> TradeSignal:
        return cls(direction="FLAT")

    @classmethod
    def exit(cls, reason: str) -> TradeSignal:
        return cls(direction="EXIT", metadata={"reason": reason})


class BaseStrategy(ABC):
    """
    on_bar(symbol, bars) receives only CLOSED bars up to and including the
    current one (look-ahead is structurally impossible).
    Expected columns: open, high, low, close, volume; UTC DatetimeIndex.
    """

    strategy_id: str = ""
    required_warmup_bars: int = 200
    holding: str = "intraday"           # "scalp" | "intraday"
    max_hold_bars: int = 48             # time stop (no EOD square-off in 24/7 markets)
    default_params: dict[str, Any] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.strategy_id}: unknown params {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    def p(self, key: str) -> Any:
        return self.params[key]

    @abstractmethod
    def on_bar(self, symbol: str, bars: pd.DataFrame) -> TradeSignal:
        """Return a TradeSignal; TradeSignal.flat() when there is nothing to do."""

    def should_exit(self, symbol: str, bars: pd.DataFrame, side: str,
                    entry_price: float, bars_held: int) -> TradeSignal | None:
        """Discretionary exit hook (beyond SL/TP). Default: time stop only."""
        if bars_held >= self.max_hold_bars:
            return TradeSignal.exit("time_stop")
        return None

    def reset(self, symbol: str | None = None) -> None:
        """Clear any per-symbol cached state before a new run."""
