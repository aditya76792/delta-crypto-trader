"""
Paper exchange: simulated Delta-style matching on top of REAL live market data.

Fidelity features
  * market orders walk the live L2 book (fallback: best bid/ask ± slippage bps)
  * limit orders rest; marketable-at-entry limits fill as taker, resting ones fill as
    maker when the opposite quote trades through, with PARTIAL fills capped by a
    participation rate of displayed size at the top of book
  * reduce-only protective stops trigger on MARK price (Delta's default trigger
    method) and fill as market orders; take-profits rest as reduce-only limits
  * fees: product maker/taker rates + 18 % GST, funding charged at each funding
    timestamp on mark notional, estimated liquidation on mark price
  * duplicate client_order_id is rejected exactly like the exchange
  * state persisted to data/paper_state.json so restarts keep positions/balance
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engines.base_executor import BaseExecutor
from engines.costs import CostModel
from infrastructure.core.events import EventBus
from infrastructure.core.logger import get_logger
from infrastructure.data.market_data import MarketDataStore
from infrastructure.exchange.instruments import InstrumentRegistry
from infrastructure.models import Fill, Order, Position, TradePlan, new_id, opposite
from layers.risk.sizing import liquidation_price

log = get_logger(__name__)


class DuplicateOrderError(Exception):
    pass


class PaperExecutor(BaseExecutor):
    mode = "paper"

    def __init__(self, registry: InstrumentRegistry, store: MarketDataStore,
                 starting_equity: float = 1000.0, slippage_bps: float = 2.0,
                 costs: CostModel | None = None, state_path: Path | None = None,
                 participation: float = 0.25, funding_hours: tuple[int, ...] = (0, 8, 16),
                 clock: Callable[[], float] = time.time, bus: EventBus | None = None) -> None:
        self.registry = registry
        self.store = store
        self.costs = costs or CostModel()
        self.slippage_bps = slippage_bps
        self.participation = participation
        self.funding_hours = funding_hours
        self.clock = clock
        self.bus = bus or EventBus.get()
        self.state_path = state_path
        self.balance = starting_equity        # realised wallet balance (USD)
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._seen_client_ids: set[str] = set()
        self._last_funding_slot: dict[str, int] = {}
        self._brackets: dict[str, tuple[float | None, float | None, str, str]] = {}
        if state_path and state_path.exists():
            self._load()

    # ── account ─────────────────────────────────────────────────────────────

    def _mark(self, symbol: str) -> float | None:
        t = self.store.tickers.get(symbol)
        if t is None:
            return None
        return t.mark or t.last

    def unrealised(self) -> float:
        total = 0.0
        for sym, p in self._positions.items():
            m = self._mark(sym)
            if p.is_open and m:
                total += p.unrealised_pnl(self.registry.get(sym), m)
        return total

    def equity(self) -> float:
        return self.balance + self.unrealised()

    def used_margin(self) -> float:
        used = 0.0
        for sym, p in self._positions.items():
            if p.is_open:
                spec = self.registry.get(sym)
                used += spec.notional(abs(p.size), p.entry_price) / max(p.leverage, 1)
        for o in self._orders.values():
            if not o.is_terminal and not o.reduce_only and o.limit_price:
                spec = self.registry.get(o.symbol)
                used += spec.notional(o.unfilled, o.limit_price) / 5
        return used

    def available_margin(self) -> float:
        return max(0.0, self.equity() - self.used_margin())

    def positions(self) -> dict[str, Position]:
        return {k: v for k, v in self._positions.items() if v.is_open}

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values()
                if not o.is_terminal and (symbol is None or o.symbol == symbol)]

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    # ── order entry ─────────────────────────────────────────────────────────

    def submit(self, order: Order) -> Order:
        if order.client_order_id in self._seen_client_ids:
            raise DuplicateOrderError(order.client_order_id)
        spec = self.registry.get(order.symbol)
        if order.size < spec.min_size or order.size != int(order.size):
            order.state, order.reject_reason = "rejected", "invalid_size"
            return order
        if order.limit_price is not None:
            order.limit_price = spec.round_price(order.limit_price)
        self._seen_client_ids.add(order.client_order_id)
        order.exchange_id = new_id("paper_")
        order.state = "open"
        if order.reduce_only:
            pos = self._positions.get(order.symbol)
            if pos is None or not pos.is_open or pos.side == order.side:
                order.state, order.reject_reason = "rejected", "reduce_only_no_position"
                return order
        self._orders[order.client_order_id] = order
        self.bus.emit("ORDER_PLACED", order)
        if order.stop_price is None:
            self._try_fill(order, at_entry=True)
        self.save()
        return order

    def execute_plan(self, plan: TradePlan) -> Order:
        coid = plan.client_order_id
        if coid in self._seen_client_ids:
            raise DuplicateOrderError(coid)
        spec = self.registry.get(plan.symbol)
        entry = Order(coid, plan.symbol, plan.side, plan.contracts, plan.order_type,
                      limit_price=plan.limit_price, plan_id=plan.plan_id)
        pos = self._positions.setdefault(plan.symbol, Position(plan.symbol, mode="paper"))
        pos.leverage = plan.leverage
        # brackets are (re)attached on every entry fill, including later partial fills
        self._brackets[coid] = (plan.stop_loss, plan.take_profit, plan.strategy_id, plan.plan_id)
        self.submit(entry)
        log.info("paper_plan_executed", symbol=plan.symbol, side=plan.side,
                 contracts=plan.contracts, filled=entry.filled, avg=entry.avg_fill_price,
                 stop=plan.stop_loss, tp=plan.take_profit, est_liq=plan.metadata.get("est_liq"),
                 spec_tick=spec.tick_size)
        return entry

    def _attach_brackets(self, symbol: str, stop: float | None, tp: float | None) -> None:
        pos = self._positions[symbol]
        if not pos.is_open:
            return
        for o in self.open_orders(symbol):
            if o.reduce_only:
                o.state = "cancelled"
        exit_side = opposite(pos.side or "buy")
        size = abs(pos.size)
        if stop:
            self.submit(Order(new_id("sl_"), symbol, exit_side, size, "market_order",
                              stop_price=stop, reduce_only=True, plan_id=pos.plan_id))
        if tp:
            self.submit(Order(new_id("tp_"), symbol, exit_side, size, "limit_order",
                              limit_price=tp, reduce_only=True, post_only=False,
                              plan_id=pos.plan_id))

    def modify_order(self, client_order_id: str, *, limit_price: float | None = None,
                     size: int | None = None, stop_price: float | None = None) -> Order:
        o = self._orders[client_order_id]
        if o.is_terminal:
            raise ValueError(f"order {client_order_id} is {o.state}")
        spec = self.registry.get(o.symbol)
        if limit_price is not None:
            o.limit_price = spec.round_price(limit_price)
        if stop_price is not None:
            o.stop_price = spec.round_price(stop_price)
        if size is not None:
            if size < o.filled:
                raise ValueError("cannot reduce size below filled quantity")
            o.size = size
        o.updated_at = datetime.now(timezone.utc)
        self.bus.emit("ORDER_UPDATE", o)
        self.save()
        return o

    def cancel_order(self, client_order_id: str) -> bool:
        o = self._orders.get(client_order_id)
        if o is None or o.is_terminal:
            return False
        o.state = "cancelled"
        self.bus.emit("ORDER_CANCELLED", o)
        self.save()
        return True

    def cancel_all(self, symbol: str | None = None) -> int:
        n = 0
        for o in self.open_orders(symbol):
            n += self.cancel_order(o.client_order_id)
        return n

    def close_position(self, symbol: str, reason: str) -> Order | None:
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open:
            return None
        self.cancel_all(symbol)
        o = Order(new_id("close_"), symbol, opposite(pos.side or "buy"), abs(pos.size),
                  "market_order", reduce_only=True)
        self.submit(o)
        log.info("paper_position_closed", symbol=symbol, reason=reason, price=o.avg_fill_price)
        return o

    # ── matching ────────────────────────────────────────────────────────────

    def on_market(self, symbol: str) -> None:
        self._charge_funding(symbol)
        if self._check_liquidation(symbol):
            return
        for o in sorted(self.open_orders(symbol), key=lambda x: x.stop_price is None):
            self._try_fill(o, at_entry=False)
        self.save()

    def _try_fill(self, o: Order, at_entry: bool) -> None:
        t = self.store.tickers.get(o.symbol)
        if t is None:
            if at_entry:
                o.state, o.reject_reason = "rejected", "no_market_data"
                self.bus.emit("ORDER_REJECTED", o)
            return
        self.registry.get(o.symbol)
        book = self.store.books.get(o.symbol)
        bid = t.best_bid or (book.best_bid if book else None) or t.last
        ask = t.best_ask or (book.best_ask if book else None) or t.last
        mark = t.mark or t.last

        if o.stop_price is not None:   # stop-market, triggered on mark
            pos = self._positions.get(o.symbol)
            if pos is None or not pos.is_open:
                o.state = "cancelled"
                return
            triggered = (mark <= o.stop_price) if o.side == "sell" else (mark >= o.stop_price)
            if not triggered:
                return
            o.size = min(o.size, abs(pos.size))
            px = self.costs.market_fill_price(o.side, bid if o.side == "sell" else ask,
                                              o.unfilled, book, self.slippage_bps)
            self._fill(o, o.unfilled, px, is_maker=False)
            return

        if o.order_type == "market_order":
            ref = ask if o.side == "buy" else bid
            px = self.costs.market_fill_price(o.side, ref, o.unfilled, book, self.slippage_bps)
            self._fill(o, o.unfilled, px, is_maker=False)
            return

        # limit order
        lp = o.limit_price or 0.0
        crosses = (ask <= lp) if o.side == "buy" else (bid >= lp)
        if not crosses:
            return
        if at_entry:   # marketable limit → taker at best price up to the limit
            if o.post_only:
                o.state, o.reject_reason = "cancelled", "post_only_would_cross"
                return
            px = min(ask, lp) if o.side == "buy" else max(bid, lp)
            self._fill(o, o.unfilled, px, is_maker=False)
            return
        # resting → maker; partial fill limited by displayed size at top of book
        levels = (book.asks if o.side == "buy" else book.bids) if book else []
        avail = sum(lv.size for lv in levels
                    if (lv.price <= lp if o.side == "buy" else lv.price >= lp)) if levels else o.unfilled
        qty = max(1, min(o.unfilled, int(avail * self.participation) or 1))
        self._fill(o, qty, lp, is_maker=True)

    def _fill(self, o: Order, qty: int, price: float, is_maker: bool) -> None:
        if qty <= 0:
            return
        spec = self.registry.get(o.symbol)
        pos = self._positions.setdefault(o.symbol, Position(o.symbol, mode="paper"))
        if o.reduce_only:
            qty = min(qty, abs(pos.size))
            if qty <= 0:
                o.state = "cancelled"
                return
        fee = self.costs.fee(spec, spec.notional(qty, price), is_maker)
        was_open = pos.is_open
        realised = pos.apply_fill(spec, o.side, qty, price)
        pos.fees_paid += fee
        self.balance += realised - fee
        prev = o.filled
        o.filled += qty
        o.avg_fill_price = ((o.avg_fill_price or 0) * prev + price * qty) / o.filled
        o.fees_paid += fee
        o.state = "closed" if o.unfilled == 0 else "partially_filled"
        o.updated_at = datetime.now(timezone.utc)
        f = Fill(o.client_order_id, o.symbol, o.side, qty, price, fee, is_maker)
        self._fills.append(f)
        self.bus.emit("ORDER_FILLED" if o.state == "closed" else "ORDER_PARTIALLY_FILLED", o)
        if not was_open and pos.is_open:
            self.bus.emit("POSITION_OPENED", pos)
        if was_open and not pos.is_open:
            self.bus.emit("POSITION_CLOSED", {"symbol": o.symbol, "realised": pos.realised_pnl,
                                              "fees": pos.fees_paid, "funding": pos.funding_paid})
            for other in self.open_orders(o.symbol):   # OCO: kill sibling brackets
                if other.reduce_only:
                    other.state = "cancelled"
            self._positions[o.symbol] = Position(o.symbol, mode="paper", leverage=pos.leverage)
        elif pos.is_open and not o.reduce_only and o.client_order_id in self._brackets:
            stop, tp, strat, plan_id = self._brackets[o.client_order_id]
            pos.stop_loss, pos.take_profit = stop, tp
            pos.strategy_id, pos.plan_id = strat, plan_id
            self._attach_brackets(o.symbol, stop, tp)
        elif pos.is_open and o.reduce_only:
            # partial exit: shrink sibling reduce-only orders to remaining size
            for other in self.open_orders(o.symbol):
                if other.reduce_only and other is not o:
                    other.size = min(other.size, other.filled + abs(pos.size))

    # ── funding & liquidation ───────────────────────────────────────────────

    def _charge_funding(self, symbol: str) -> None:
        pos = self._positions.get(symbol)
        spec = self.registry.get(symbol)
        if pos is None or not pos.is_open or not spec.is_perpetual:
            return
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        if now.hour not in self.funding_hours:
            return
        slot = int(self.clock() // 3600)
        if self._last_funding_slot.get(symbol) == slot:
            return
        self._last_funding_slot[symbol] = slot
        t = self.store.tickers.get(symbol)
        rate = (t.funding_rate if t and t.funding_rate is not None else 0.0)
        mark = self._mark(symbol) or pos.entry_price
        pay = self.costs.funding_payment(pos.side or "buy", spec.notional(abs(pos.size), mark), rate)
        pos.funding_paid += pay
        self.balance -= pay
        self.bus.emit("FUNDING", {"symbol": symbol, "rate": rate, "paid": pay})
        log.info("paper_funding", symbol=symbol, rate=rate, paid=round(pay, 6))

    def _check_liquidation(self, symbol: str) -> bool:
        pos = self._positions.get(symbol)
        mark = self._mark(symbol)
        if pos is None or not pos.is_open or not mark:
            return False
        spec = self.registry.get(symbol)
        liq = liquidation_price(pos.side or "buy", pos.entry_price, pos.leverage,
                                spec.maintenance_margin)
        hit = mark <= liq if pos.side == "buy" else mark >= liq
        if not hit:
            return False
        margin = spec.notional(abs(pos.size), pos.entry_price) / max(pos.leverage, 1)
        self.balance -= margin
        log.critical("paper_liquidated", symbol=symbol, mark=mark, liq=liq, lost=margin)
        for o in self.open_orders(symbol):
            o.state = "cancelled"
        self.bus.emit("POSITION_CLOSED", {"symbol": symbol, "realised": -margin,
                                          "reason": "liquidation"})
        self._positions[symbol] = Position(symbol, mode="paper", leverage=pos.leverage)
        return True

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "balance": self.balance,
            "positions": {s: {k: (v.isoformat() if isinstance(v, datetime) else v)
                              for k, v in p.__dict__.items()}
                          for s, p in self._positions.items() if p.is_open},
            "orders": [{**{k: (v.isoformat() if isinstance(v, datetime) else v)
                           for k, v in o.__dict__.items()}} for o in self.open_orders()],
            "seen_client_ids": sorted(self._seen_client_ids)[-5000:],
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str))
        tmp.replace(self.state_path)

    def _load(self) -> None:
        d = json.loads(self.state_path.read_text())   # type: ignore[union-attr]
        self.balance = float(d.get("balance", self.balance))
        for s, pd_ in d.get("positions", {}).items():
            pd_.pop("opened_at", None)
            self._positions[s] = Position(**{k: v for k, v in pd_.items()
                                             if k in Position.__dataclass_fields__})
        for od in d.get("orders", []):
            for k in ("created_at", "updated_at"):
                od.pop(k, None)
            o = Order(**{k: v for k, v in od.items() if k in Order.__dataclass_fields__})
            self._orders[o.client_order_id] = o
        self._seen_client_ids = set(d.get("seen_client_ids", []))
        log.info("paper_state_loaded", balance=self.balance, positions=len(self._positions))

    def reset_state(self) -> None:
        if self.state_path and self.state_path.exists():
            self.state_path.unlink()
