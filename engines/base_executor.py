"""
Common executor interface (paper and live are interchangeable behind it).

The trader loop only talks to this interface, so paper trading exercises exactly
the same decision path as live trading. Paper and live live in separate modules
and live can only be constructed through ``engines.live_executor.factory``,
which enforces the real-money gates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from infrastructure.models import Order, Position, TradePlan


class BaseExecutor(ABC):
    mode: str = "abstract"

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def available_margin(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    def position(self, symbol: str) -> Position:
        return self.positions().get(symbol) or Position(symbol=symbol, mode=self.mode)

    @abstractmethod
    def open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abstractmethod
    def execute_plan(self, plan: TradePlan) -> Order:
        """Submit entry + protective stop (+ take-profit). Must be idempotent per plan."""

    @abstractmethod
    def modify_order(self, client_order_id: str, *, limit_price: float | None = None,
                     size: int | None = None, stop_price: float | None = None) -> Order: ...

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> bool: ...

    @abstractmethod
    def cancel_all(self, symbol: str | None = None) -> int: ...

    @abstractmethod
    def close_position(self, symbol: str, reason: str) -> Order | None: ...

    def flatten_all(self, reason: str) -> int:
        self.cancel_all()
        n = 0
        for sym, pos in list(self.positions().items()):
            if pos.is_open and self.close_position(sym, reason) is not None:
                n += 1
        return n

    def reconcile(self) -> list[dict[str, Any]]:
        return []

    def on_market(self, symbol: str) -> None:
        """Called after market data for ``symbol`` updates (paper fills, funding)."""

    def handle_ws(self, msg: dict[str, Any]) -> None:
        """Private WS messages (live)."""

    def exposure(self, mark_of: Any) -> tuple[dict[str, float], float]:
        per: dict[str, float] = {}
        for sym, pos in self.positions().items():
            if pos.is_open:
                per[sym] = abs(mark_of(sym, pos))
        return per, sum(per.values())

    def save(self) -> None:
        """Persist state (paper)."""
