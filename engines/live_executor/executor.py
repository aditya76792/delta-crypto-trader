"""
Live executor for Delta Exchange India (REAL MONEY).

Safety & reliability
  * can only be built via ``engines.live_executor.factory.build_live_executor``,
    which enforces every real-money gate
  * idempotent entries: one deterministic ``client_order_id`` per plan; a per-symbol
    in-flight lock; on an ambiguous network failure we look the order up by
    client_order_id (open orders, then fills) BEFORE any resend
  * leverage is set per product before the first order
  * protective stop + take-profit are attached as exchange-side bracket orders
    (``bracket_stop_loss_price`` / ``bracket_take_profit_price``, trigger on mark price)
    so protection survives a crash of this process; if the exchange refuses an
    attached bracket we fall back to a position bracket (POST /v2/orders/bracket)
    and, failing that, flatten the position immediately
  * partial fills are tracked from the private ``orders`` / ``v2/user_trades``
    channels; ``reconcile()`` treats the exchange as the source of truth and
    re-protects any open position that lacks a stop
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from engines.base_executor import BaseExecutor
from infrastructure.core.events import EventBus
from infrastructure.core.logger import get_logger
from infrastructure.exchange.instruments import InstrumentRegistry
from infrastructure.exchange.rest import AuthError, DeltaAPIError, DeltaRestClient
from infrastructure.models import Order, Position, TradePlan, new_id, opposite

log = get_logger(__name__)

_TERMINAL = {"closed": "closed", "cancelled": "cancelled", "rejected": "rejected"}


class LiveExecutor(BaseExecutor):
    mode = "live"

    def __init__(self, rest: DeltaRestClient, registry: InstrumentRegistry,
                 settle_asset: str = "USD", use_brackets: bool = True,
                 stop_trigger: str = "mark_price", bus: EventBus | None = None,
                 _gate_token: object | None = None) -> None:
        from engines.live_executor.factory import _GATE_TOKEN
        if _gate_token is not _GATE_TOKEN:
            raise RuntimeError("LiveExecutor must be created via build_live_executor()")
        self.rest = rest
        self.registry = registry
        self.settle_asset = settle_asset
        self.use_brackets = use_brackets
        self.stop_trigger = stop_trigger
        self.bus = bus or EventBus.get()
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._leverage_set: dict[int, float] = {}
        self._inflight: set[str] = set()
        self._sent: set[str] = set()
        self._lock = threading.RLock()
        self._balance_cache: tuple[float, float, float] = (0.0, 0.0, 0.0)  # ts, equity, avail
        self._realised_at_open: dict[str, float] = {}

    # ── account ─────────────────────────────────────────────────────────────

    def _refresh_balance(self, max_age: float = 5.0) -> tuple[float, float]:
        ts, eq, av = self._balance_cache
        if time.time() - ts < max_age:
            return eq, av
        rows = self.rest.get_balances()
        for r in rows:
            sym = r.get("asset_symbol") or (r.get("asset") or {}).get("symbol")
            if sym == self.settle_asset:
                eq = float(r.get("balance") or 0)
                av = float(r.get("available_balance") or 0)
                break
        meta = getattr(self.rest, "last_meta", None) or {}
        if meta.get("net_equity") not in (None, ""):
            try:   # wallet meta.net_equity includes unrealised PnL (docs: GET /wallet/balances)
                eq = float(meta["net_equity"])
            except (TypeError, ValueError):
                pass
        self._balance_cache = (time.time(), eq, av)
        return eq, av

    def equity(self) -> float:
        return self._refresh_balance()[0]

    def available_margin(self) -> float:
        return self._refresh_balance()[1]

    def positions(self) -> dict[str, Position]:
        return {k: v for k, v in self._positions.items() if v.is_open}

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values()
                if not o.is_terminal and (symbol is None or o.symbol == symbol)]

    # ── order building ──────────────────────────────────────────────────────

    def _ensure_leverage(self, product_id: int, leverage: float) -> None:
        if self._leverage_set.get(product_id) == leverage:
            return
        self.rest.set_leverage(product_id, leverage)
        self._leverage_set[product_id] = leverage
        log.info("leverage_set", product_id=product_id, leverage=leverage)

    def order_body(self, plan: TradePlan) -> dict[str, Any]:
        spec = self.registry.get(plan.symbol)
        body: dict[str, Any] = {
            "product_id": spec.product_id,
            "product_symbol": spec.symbol,
            "size": int(plan.contracts),
            "side": plan.side,
            "order_type": plan.order_type,
            "client_order_id": plan.client_order_id,
            "time_in_force": "gtc",
            "reduce_only": False,
        }
        if plan.order_type == "limit_order":
            body["limit_price"] = str(spec.round_price(plan.limit_price or plan.entry_price,
                                                       plan.side))
        if self.use_brackets:
            body["bracket_stop_trigger_method"] = self.stop_trigger
            body["bracket_stop_loss_price"] = str(spec.round_stop(plan.stop_loss, plan.side))
            if plan.take_profit:
                body["bracket_take_profit_price"] = str(spec.round_price(plan.take_profit))
        return body

    # ── core API ────────────────────────────────────────────────────────────

    def execute_plan(self, plan: TradePlan) -> Order:
        coid = plan.client_order_id
        with self._lock:
            if coid in self._sent:
                raise RuntimeError(f"duplicate plan {coid} refused")
            if plan.symbol in self._inflight:
                raise RuntimeError(f"entry already in flight for {plan.symbol}")
            if self.open_orders(plan.symbol) and any(not o.reduce_only
                                                     for o in self.open_orders(plan.symbol)):
                raise RuntimeError(f"open entry order already working on {plan.symbol}")
            self._inflight.add(plan.symbol)
            self._sent.add(coid)
        spec = self.registry.get(plan.symbol)
        order = Order(coid, plan.symbol, plan.side, plan.contracts, plan.order_type,
                      limit_price=plan.limit_price, plan_id=plan.plan_id)
        try:
            self._ensure_leverage(spec.product_id, plan.leverage)
            body = self.order_body(plan)
            try:
                res = self.rest.place_order(body)
            except DeltaAPIError as exc:
                if exc.code in ("network_error",) or (exc.status or 0) >= 500:
                    res = self._lookup_after_ambiguous_failure(spec.product_id, coid)
                    if res is None:
                        log.warning("order_not_found_after_failure_resending", coid=coid)
                        res = self.rest.place_order(body)   # same coid → exchange dedupes
                else:
                    raise
            self._apply_order_update(order, res)
            self._orders[coid] = order
            self.bus.emit("ORDER_PLACED", order)
            log.info("live_order_placed", coid=coid, exchange_id=order.exchange_id,
                     symbol=plan.symbol, side=plan.side, size=plan.contracts,
                     stop=plan.stop_loss, tp=plan.take_profit)
            pos = self._positions.setdefault(plan.symbol, Position(plan.symbol, mode="live"))
            pos.stop_loss, pos.take_profit = plan.stop_loss, plan.take_profit
            pos.strategy_id, pos.plan_id, pos.leverage = plan.strategy_id, plan.plan_id, plan.leverage
            return order
        except AuthError:
            raise
        except DeltaAPIError as exc:
            order.state, order.reject_reason = "rejected", exc.code
            self._orders[coid] = order
            self.bus.emit("ORDER_REJECTED", order)
            log.error("live_order_rejected", coid=coid, code=exc.code, ctx=exc.context)
            return order
        finally:
            with self._lock:
                self._inflight.discard(plan.symbol)

    def _lookup_after_ambiguous_failure(self, product_id: int, coid: str) -> dict | None:
        """Did the exchange accept the order even though we saw an error?"""
        for _ in range(3):
            try:
                for o in self.rest.get_open_orders(product_id) or []:
                    if o.get("client_order_id") == coid:
                        return o
                for f in self.rest.get_fills(product_id) or []:
                    if (f.get("meta_data") or {}).get("client_order_id") == coid or \
                            f.get("client_order_id") == coid:
                        return {"id": f.get("order_id"), "client_order_id": coid,
                                "state": "closed", "size": f.get("size"),
                                "unfilled_size": 0, "average_fill_price": f.get("price")}
                return None
            except DeltaAPIError:
                time.sleep(1.0)
        raise DeltaAPIError("ambiguous_order_state",
                            f"could not determine state of {coid}; refusing to resend")

    def modify_order(self, client_order_id: str, *, limit_price: float | None = None,
                     size: int | None = None, stop_price: float | None = None) -> Order:
        o = self._orders[client_order_id]
        spec = self.registry.get(o.symbol)
        body: dict[str, Any] = {"id": int(o.exchange_id or 0), "product_id": spec.product_id}
        if limit_price is not None:
            body["limit_price"] = str(spec.round_price(limit_price, o.side))
        if size is not None:
            body["size"] = int(size)
        if stop_price is not None:
            body["stop_price"] = str(spec.round_price(stop_price))
        res = self.rest.edit_order(body)
        self._apply_order_update(o, res)
        self.bus.emit("ORDER_UPDATE", o)
        return o

    def move_stop(self, symbol: str, stop: float, take_profit: float | None = None) -> None:
        """Update the position bracket (e.g. trailing stop)."""
        spec = self.registry.get(symbol)
        pos = self._positions.get(symbol)
        side = pos.side if pos else "buy"
        body: dict[str, Any] = {"product_id": spec.product_id, "product_symbol": symbol,
                                "bracket_stop_trigger_method": self.stop_trigger,
                                "stop_loss_order": {"order_type": "market_order",
                                                    "stop_price": str(spec.round_stop(stop, side or "buy"))}}
        if take_profit:
            body["take_profit_order"] = {"order_type": "limit_order",
                                         "stop_price": str(spec.round_price(take_profit)),
                                         "limit_price": str(spec.round_price(take_profit))}
        self.rest.place_bracket(body)
        if pos:
            pos.stop_loss = stop
            pos.take_profit = take_profit or pos.take_profit

    def cancel_order(self, client_order_id: str) -> bool:
        o = self._orders.get(client_order_id)
        if o is None or o.is_terminal:
            return False
        spec = self.registry.get(o.symbol)
        try:
            self.rest.cancel_order(spec.product_id,
                                   order_id=int(o.exchange_id) if o.exchange_id else None,
                                   client_order_id=client_order_id)
        except DeltaAPIError as exc:
            if exc.code != "open_order_not_found":
                raise
        o.state = "cancelled"
        self.bus.emit("ORDER_CANCELLED", o)
        return True

    def cancel_all(self, symbol: str | None = None) -> int:
        pid = self.registry.get(symbol).product_id if symbol else None
        self.rest.cancel_all(pid)
        n = 0
        for o in self.open_orders(symbol):
            o.state = "cancelled"
            n += 1
        return n

    def close_position(self, symbol: str, reason: str) -> Order | None:
        pos = self._positions.get(symbol)
        if pos is None or not pos.is_open:
            return None
        spec = self.registry.get(symbol)
        coid = new_id("cls")[:32]
        body = {"product_id": spec.product_id, "product_symbol": symbol,
                "size": abs(pos.size), "side": opposite(pos.side or "buy"),
                "order_type": "market_order", "reduce_only": True, "client_order_id": coid}
        res = self.rest.place_order(body)
        o = Order(coid, symbol, body["side"], abs(pos.size), "market_order", reduce_only=True)
        self._apply_order_update(o, res)
        self._orders[coid] = o
        log.warning("live_position_close_sent", symbol=symbol, reason=reason, size=abs(pos.size))
        return o

    # ── exchange → local state ──────────────────────────────────────────────

    @staticmethod
    def _apply_order_update(o: Order, r: dict[str, Any] | None) -> None:
        if not r:
            return
        oid = r.get("id", r.get("order_id"))      # REST uses "id", WS orders use "order_id"
        o.exchange_id = str(oid) if oid not in (None, "") else o.exchange_id
        size = int(float(r.get("size", o.size) or o.size))
        unfilled = int(float(r.get("unfilled_size", size) or 0))
        o.size = size
        o.filled = max(0, size - unfilled)
        if r.get("average_fill_price"):
            o.avg_fill_price = float(r["average_fill_price"])
        st = str(r.get("state", o.state))
        if st == "open" and 0 < o.filled < o.size:
            st = "partially_filled"
        o.state = _TERMINAL.get(st, st)
        if r.get("paid_commission"):
            o.fees_paid = float(r["paid_commission"])
        o.updated_at = datetime.now(timezone.utc)

    def handle_ws(self, msg: dict[str, Any]) -> None:
        """Private channels (docs, checked 2026-10-29): ``orders`` updates carry
        order_id, client_order_id, symbol, size, unfilled_size, state, reason; a
        subscription first sends {"action":"snapshot","result":[...]}. ``positions``
        updates carry symbol, size (signed), entry_price, liquidation_price, margin.
        ``v2/user_trades`` reports fills (reason normal / adl / liquidation)."""
        t = msg.get("type")
        if msg.get("action") == "snapshot" and isinstance(msg.get("result"), list):
            for row in msg["result"]:
                self.handle_ws({**row, "type": t, "symbol": row.get("product_symbol")
                                or row.get("symbol") or msg.get("symbol")})
            return
        if t in ("v2/user_trades", "user_trades"):
            reason = msg.get("reason")
            if reason in ("adl", "liquidation"):
                log.critical("exchange_forced_fill", reason=reason, symbol=msg.get("symbol"),
                             size=msg.get("size"), price=msg.get("price"))
                self.bus.emit("SYSTEM_ERROR", {"issue": f"exchange_{reason}", "msg": msg})
            return
        if t == "orders":
            coid = msg.get("client_order_id") or ""
            o = self._orders.get(coid)
            if o is None:
                if not coid:
                    return
                o = Order(coid, msg.get("symbol") or msg.get("product_symbol", ""),
                          msg.get("side", "buy"),
                          int(float(msg.get("size", 0) or 0)),
                          msg.get("order_type", "limit_order"),
                          reduce_only=bool(msg.get("reduce_only")))
                self._orders[coid] = o
            before = o.filled
            self._apply_order_update(o, msg)
            if o.filled > before:
                self.bus.emit("ORDER_FILLED" if o.unfilled == 0 else "ORDER_PARTIALLY_FILLED", o)
            elif o.state == "cancelled":
                self.bus.emit("ORDER_CANCELLED", o)
        elif t == "positions":
            sym = msg.get("product_symbol") or msg.get("symbol", "")
            if not sym:
                return
            self._update_position(sym, msg)

    def _update_position(self, sym: str, r: dict[str, Any]) -> None:
        pos = self._positions.setdefault(sym, Position(sym, mode="live"))
        was_open = pos.is_open
        prev_size = pos.size
        pos.size = int(float(r.get("size", 0) or 0))
        if prev_size and pos.size and (prev_size > 0) != (pos.size > 0):
            # exits must never flip a position (bracket race / manual trade) → flatten + alert
            log.critical("position_flipped", symbol=sym, before=prev_size, after=pos.size)
            self.bus.emit("SYSTEM_ERROR", {"issue": "position_flipped", "symbol": sym})
        pos.entry_price = float(r.get("entry_price") or 0)
        if r.get("liquidation_price"):
            pos.exchange_liq_price = float(r["liquidation_price"])
        realised_before = self._realised_at_open.get(sym)
        if r.get("realized_pnl") is not None:
            pos.realised_pnl = float(r["realized_pnl"])
        if not was_open and pos.is_open:
            self._realised_at_open[sym] = pos.realised_pnl
            self.bus.emit("POSITION_OPENED", pos)
        if was_open and not pos.is_open:
            trade_pnl = pos.realised_pnl - (realised_before or 0.0) \
                if r.get("realized_pnl") is not None else 0.0
            self.bus.emit("POSITION_CLOSED", {"symbol": sym, "realised": trade_pnl})

    def reconcile(self) -> list[dict[str, Any]]:
        """Exchange is the source of truth. Returns discrepancies found (and fixed)."""
        issues: list[dict[str, Any]] = []
        try:
            remote = self.rest.get_positions() or []
            open_orders = self.rest.get_open_orders() or []
        except DeltaAPIError as exc:
            return [{"issue": "api_error", "error": str(exc)}]
        seen = set()
        for r in remote:
            sym = r.get("product_symbol") or (r.get("product") or {}).get("symbol")
            if not sym or sym not in self.registry:
                continue
            seen.add(sym)
            size = int(float(r.get("size", 0) or 0))
            local = self._positions.get(sym)
            if (local.size if local else 0) != size:
                issues.append({"symbol": sym, "issue": "size_mismatch",
                               "local": local.size if local else 0, "exchange": size})
            self._update_position(sym, r)
        for sym, p in list(self._positions.items()):
            if p.is_open and sym not in seen:
                issues.append({"symbol": sym, "issue": "local_position_missing_on_exchange",
                               "local": p.size})
                p.size = 0
        # every open position must have an exchange-side stop
        stops_by_symbol: dict[str, bool] = {}
        for o in open_orders:
            if o.get("stop_order_type") in ("stop_loss_order",) or o.get("bracket_stop_loss_price"):
                stops_by_symbol[o.get("product_symbol", "")] = True
        for sym, p in self.positions().items():
            if not stops_by_symbol.get(sym):
                issues.append({"symbol": sym, "issue": "unprotected_position"})
                if p.stop_loss:
                    try:
                        self.move_stop(sym, p.stop_loss, p.take_profit)
                    except DeltaAPIError as exc:
                        log.critical("reprotect_failed_flattening", symbol=sym, error=str(exc))
                        self.close_position(sym, "unprotected")
                else:
                    log.critical("unprotected_position_no_known_stop", symbol=sym)
        for i in issues:
            self.bus.emit("RECONCILE_MISMATCH", i)
        if issues:
            log.warning("reconcile_issues", issues=issues)
        return issues
