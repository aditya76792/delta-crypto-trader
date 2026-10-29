"""
Trader: the real-time decision loop shared by paper and live mode.

    WS message ─► MarketDataStore ─► (candle closed) ─► Strategy ─► Planner
         │                                                          │
         └─► executor.on_market / handle_ws          RiskChecker ◄──┘
                                                         │ approved
                                                         ▼
                                                 executor.execute_plan

Watchdogs (every few seconds): kill-switch file, UTC-day loss limit, stale data,
WebSocket health and — in live mode — exchange reconciliation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from engines.base_executor import BaseExecutor
from engines.costs import CostModel
from infrastructure.config.settings import Settings
from infrastructure.core.events import Event, EventBus
from infrastructure.core.logger import get_logger
from infrastructure.data.market_data import MarketDataStore
from infrastructure.exchange.instruments import InstrumentRegistry
from infrastructure.models import Candle, Position
from infrastructure.workspace import now_iso, path, read_json, write_json
from layers.planning import build_plan
from layers.risk.checker import CheckContext, RiskChecker
from layers.risk.state import RiskState
from layers.technical.indicators import atr as atr_ind
from strategies.base import BaseStrategy

log = get_logger(__name__)
LOOKBACK_BARS = 1000


class Trader:
    def __init__(self, settings: Settings, registry: InstrumentRegistry, store: MarketDataStore,
                 executor: BaseExecutor, strategy: BaseStrategy, risk_state: RiskState,
                 bus: EventBus | None = None, clock: Callable[[], float] = time.time,
                 costs: CostModel | None = None) -> None:
        self.s = settings
        self.registry = registry
        self.store = store
        self.ex = executor
        self.strategy = strategy
        self.rs = risk_state
        self.bus = bus or EventBus.get()
        self.clock = clock
        self.costs = costs or CostModel()
        self.checker = RiskChecker(settings.risk, self.costs)
        self.halted = False
        self.halt_reason = ""
        self.confirm_entries = False          # live: every entry waits for a typed yes
        self.confirm_timeout_s = 120.0
        self.pending: dict[str, dict[str, Any]] = {}
        self._bars_held: dict[str, int] = {}
        self.decisions: list[dict[str, Any]] = []
        self.bus.subscribe("POSITION_CLOSED", self._on_position_closed)
        self.bus.subscribe("POSITION_OPENED", self._on_position_opened)

    # ── events ──────────────────────────────────────────────────────────────

    def _on_position_opened(self, ev: Event) -> None:
        pos: Position = ev.payload
        self._bars_held[pos.symbol] = 0

    def _on_position_closed(self, ev: Event) -> None:
        p = ev.payload or {}
        net = float(p.get("realised", 0)) - float(p.get("fees", 0)) - float(p.get("funding", 0))
        self.rs.record_trade_result(net, self.s.risk.max_consecutive_losses,
                                    self.s.risk.cooldown_minutes_after_loss_streak)
        self._bars_held.pop(p.get("symbol", ""), None)
        log.info("trade_closed", symbol=p.get("symbol"), net=round(net, 6),
                 reason=p.get("reason", ""), loss_streak=self.rs.loss_streak)

    # ── message entry point ─────────────────────────────────────────────────

    def handle_message(self, msg: dict[str, Any]) -> None:
        mtype = str(msg.get("type", ""))
        if mtype in ("key-auth", "auth"):
            if msg.get("success") is False:
                self.halt(f"websocket auth failed: {msg.get('status')}", flatten=False)
            return
        if mtype in ("orders", "positions", "v2/user_trades", "user_trades", "margins"):
            self.ex.handle_ws(msg)
            self.bus.process_all()
            return
        closed = self.store.handle(msg)
        sym = str(msg.get("symbol", "")).replace("MARK:", "")
        sym = sym or str(msg.get("sy", "")).replace("MARK:", "")
        if not sym and mtype == "ticker" and msg.get("d"):
            sym = msg["d"][0].get("s", "")
        if sym and mtype in ("ticker", "v2/ticker", "ob_l2", "l2_orderbook", "mark_price",
                             "funding_rate", "trades"):
            self.ex.on_market(sym)
        self.bus.process_all()
        for s, candle in closed:
            if s in self.s.symbols:
                self.on_candle_close(s, candle)
        self.bus.process_all()

    # ── decisions ───────────────────────────────────────────────────────────

    def on_candle_close(self, symbol: str, candle: Candle) -> None:
        self.watchdog()
        bars = self.store.bars(symbol).tail(LOOKBACK_BARS)
        pos = self.ex.position(symbol)
        if pos.is_open:
            self._bars_held[symbol] = self._bars_held.get(symbol, 0) + 1
            ex = self.strategy.should_exit(symbol, bars, pos.side or "buy", pos.entry_price,
                                           self._bars_held[symbol])
            if ex is not None:
                self._record(symbol, "exit", ex.metadata.get("reason", ""))
                self.ex.close_position(symbol, ex.metadata.get("reason", "strategy_exit"))
            return
        if self.halted:
            return
        if len(bars) < self.strategy.required_warmup_bars:
            return
        sig = self.strategy.on_bar(symbol, bars)
        if not sig.is_actionable:
            return
        self.try_enter(symbol, sig, bars)

    def try_enter(self, symbol: str, sig: Any, bars: Any) -> None:
        spec = self.registry.get(symbol)
        t = self.store.tickers.get(symbol)
        if t is None:
            self._record(symbol, "veto", "no_ticker")
            return
        ref = t.mark or t.last
        equity = self.ex.equity()
        self.rs.roll_day_if_needed(equity)
        per_sym, total = self.ex.exposure(lambda s, p: self.registry.get(s).notional(
            abs(p.size), (self.store.tickers[s].mark or self.store.tickers[s].last)
            if s in self.store.tickers else p.entry_price))
        pr = build_plan(sig, spec, ref, equity, self.ex.available_margin(),
                        self.s.default_leverage, self.s.risk, self.strategy.strategy_id,
                        per_sym.get(symbol, 0.0), total, self.costs)
        if pr.plan is None:
            self._record(symbol, "skip", pr.reason)
            return
        kill, why = self.rs.kill_switch_active()
        a = atr_ind(bars.tail(200), 14).iloc[-1] if len(bars) > 20 else None
        ctx = CheckContext(
            equity=equity, day_start_equity=self.rs.day_start_equity or equity,
            last_price=t.last, mark_price=t.mark, funding_rate=t.funding_rate,
            spread_pct=t.spread_pct, data_age_s=self.store.age(symbol, "ticker"),
            atr=float(a) if a is not None else None,
            open_position_count=len(self.ex.positions()),
            symbol_exposure=per_sym.get(symbol, 0.0), total_exposure=total,
            kill_switch=kill, kill_reason=why, cooldown=self.rs.in_cooldown(),
            loss_streak=self.rs.loss_streak, orders_last_minute=self.rs.orders_last_minute())
        res = self.checker.check(pr.plan, spec, ctx)
        if not res.approved:
            self._record(symbol, "veto", res.summary())
            self.bus.emit("PLAN_VETOED", {"plan": pr.plan, "vetoes": res.veto_ids})
            log.info("plan_vetoed", symbol=symbol, vetoes=res.veto_ids)
            return
        if self.confirm_entries:
            pid = pr.plan.plan_id[-8:]
            self.pending[pid] = {"plan": pr.plan, "signal": sig, "created": self.clock()}
            write_json(path(f"live/pending/{pid}.json"), {
                "id": pid, "symbol": symbol, "side": pr.plan.side, "contracts": pr.plan.contracts,
                "entry_ref": pr.plan.entry_price, "stop": pr.plan.stop_loss,
                "target": pr.plan.take_profit, "leverage": pr.plan.leverage,
                "expires_in_s": self.confirm_timeout_s, "created_at": now_iso(),
                "confirm_with": f"python -m main confirm {pid}"})
            self._record(symbol, "pending_confirm", pid)
            log.warning("entry_waiting_for_confirmation", id=pid, symbol=symbol,
                        side=pr.plan.side, contracts=pr.plan.contracts)
            return
        self._execute(symbol, pr.plan)

    def _execute(self, symbol: str, plan: Any) -> None:
        self.bus.emit("PLAN_APPROVED", plan)
        self.rs.note_order()
        try:
            order = self.ex.execute_plan(plan)
            self._record(symbol, "order", f"{plan.side} {plan.contracts} state={order.state}")
        except Exception as exc:
            self._record(symbol, "error", str(exc))
            log.exception("execute_plan_failed", symbol=symbol, error=str(exc))
        self.bus.process_all()

    def _record(self, symbol: str, kind: str, detail: str) -> None:
        self.decisions.append({"ts": self.clock(), "symbol": symbol, "kind": kind,
                               "detail": detail})
        if len(self.decisions) > 5000:
            del self.decisions[:1000]

    # ── watchdogs ───────────────────────────────────────────────────────────

    def watchdog(self) -> None:
        equity = self.ex.equity()
        self.rs.roll_day_if_needed(equity)
        kill, why = self.rs.kill_switch_active()
        if kill and not self.halted:
            self.halt(f"kill_switch:{why}", flatten=True)
            return
        if not kill and self.halted and self.halt_reason.startswith("kill_switch"):
            self.halted, self.halt_reason = False, ""
        start = self.rs.day_start_equity
        if start > 0 and (equity - start) / start <= -self.s.risk.max_daily_loss_pct:
            if not self.halted:
                self.bus.emit("DAILY_LOSS_LIMIT_HIT", {"equity": equity, "start": start})
                self.rs.engage_kill_switch(
                    f"daily loss {(equity - start) / start:.2%} hit limit "
                    f"-{self.s.risk.max_daily_loss_pct:.2%}")
                self.halt("daily_loss_limit", flatten=True)
        for sym in self.s.symbols:
            if self.store.is_stale(sym, self.s.risk.stale_data_seconds):
                self.bus.emit("STALE_DATA", {"symbol": sym, "age": self.store.age(sym)})

    def halt(self, reason: str, flatten: bool) -> None:
        self.halted, self.halt_reason = True, reason
        log.critical("trading_halted", reason=reason, flatten=flatten)
        self.bus.emit("KILL_SWITCH", {"reason": reason})
        if flatten:
            try:
                n = self.ex.flatten_all(reason)
                log.critical("flattened", positions=n)
            except Exception as exc:
                log.exception("flatten_failed", error=str(exc))
        self.bus.process_all()

    # ── async runtime (live feed) ───────────────────────────────────────────

    def poll_candles(self, rest: Any) -> int:
        """REST fallback that closes finished bars even if the WS candle stream is
        missing or late (candles are authoritative from /v2/history/candles)."""
        n = 0
        now = int(self.clock())
        for sym in self.s.symbols:
            try:
                rows = rest.get_candles(sym, self.s.resolution, now - 3 * self.store.bar_seconds, now)
            except Exception as exc:
                log.warning("candle_poll_failed", symbol=sym, error=str(exc))
                continue
            for s_, c in self.store.close_from_rest(sym, rows):
                n += 1
                self.on_candle_close(s_, c)
        self.bus.process_all()
        return n

    async def run(self, ws_public: Any, ws_private: Any = None, rest: Any = None,
                  live: bool = False, reconcile_every_s: float = 60.0,
                  stop_after_s: float | None = None) -> None:
        """Public market data (compact channels) + optional private order/position
        stream. ``rest`` enables the candle-closing fallback poller."""
        res = self.s.resolution
        syms = self.s.symbols
        ws_public.subscribe("ticker", syms)
        ws_public.subscribe("ob_l2", syms)
        ws_public.subscribe(f"candlestick_{res}", syms)
        ws_public.subscribe("mark_price", [f"MARK:{x}" for x in syms])
        ws_public.subscribe("funding_rate", syms)
        ws_public.on_message(self.handle_message)
        runners = [ws_public.run()]
        if ws_private is not None:
            for ch in ("orders", "positions", "v2/user_trades"):
                ws_private.subscribe_private(ch)
            ws_private.on_message(self.handle_message)

            async def _on_reconnect() -> None:
                await asyncio.to_thread(self.ex.reconcile)
            ws_private.on_reconnect(_on_reconnect)
            ws_public.on_reconnect(_on_reconnect)
            runners.append(ws_private.run())
        tasks = [asyncio.create_task(r) for r in runners]
        tasks.append(asyncio.create_task(self._periodic(live, reconcile_every_s, rest)))
        try:
            if stop_after_s:
                await asyncio.sleep(stop_after_s)
            else:
                await asyncio.gather(*tasks)
        finally:
            for w in (ws_public, ws_private):
                if w is not None:
                    await w.stop()
            for t in tasks:
                t.cancel()
            self.ex.save()

    async def _periodic(self, live: bool, reconcile_every_s: float, rest: Any = None) -> None:
        last_rec = 0.0
        last_bar_poll = 0
        while True:
            await asyncio.sleep(5)
            try:
                self.watchdog()
                bar = int(self.clock()) // self.store.bar_seconds
                # a few seconds after each bar boundary, make sure the bar got closed
                if rest is not None and bar != last_bar_poll and \
                        int(self.clock()) % self.store.bar_seconds >= 3:
                    last_bar_poll = bar
                    await asyncio.to_thread(self.poll_candles, rest)
                if live and time.time() - last_rec > reconcile_every_s:
                    last_rec = time.time()
                    await asyncio.to_thread(self.ex.reconcile)
                self.on_tick()
                self.ex.save()
                self.bus.process_all()
            except Exception as exc:
                log.exception("periodic_error", error=str(exc))

    def on_tick(self) -> None:
        """Per-tick work: live entries that wait for the user's typed yes."""
        for pid, item in list(self.pending.items()):
            conf = read_json(path(f"live/confirmed/{pid}.json"))
            age = self.clock() - item["created"]
            if conf is None and age <= self.confirm_timeout_s:
                continue
            self.pending.pop(pid, None)
            plan = item["plan"]
            if conf is None:
                self._record(plan.symbol, "expired", pid)
                continue
            if conf.get("answer") != "yes":
                self._record(plan.symbol, "declined", pid)
                continue
            if self.halted or self.ex.position(plan.symbol).is_open:
                self._record(plan.symbol, "skip", f"{pid}: halted or already in a position")
                continue
            # prices moved while waiting: re-plan and re-check with fresh data before sending
            self.confirm_entries, saved = False, self.confirm_entries
            try:
                self.try_enter(plan.symbol, item["signal"], self.store.bars(plan.symbol).tail(LOOKBACK_BARS))
            finally:
                self.confirm_entries = saved
