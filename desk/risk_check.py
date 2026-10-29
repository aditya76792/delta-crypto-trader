"""
Pre-trade risk check (crypto rebuild of trader-risk): every order in
trader/orders.json gets PASS or BLOCK with reasons, checked in file order as if
each PASS had filled (a batch that is fine order by order but breaks a limit
together is caught). Verdicts are recorded by order fingerprint in
trader/state/risk.json; the broker only accepts an order whose exact fingerprint
passed today.

Exits (reduce_only) are always allowed: format errors only.
Entries run the engine's crypto rules (CR-01…CR-20: stop side, whole contracts,
risk incl. fees, daily loss, positions, exposure, leverage, liquidation distance,
reward:risk, spread, fresh quote, mark/last divergence, funding, tick alignment,
no averaging down, kill switch, loss-streak cooldown, order throttle) plus desk
limits: allowed/blocked symbols, options switch, max trades/orders per UTC day,
max daily loss in USD, require_stop, allow_short.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from engines.costs import CostModel
from infrastructure.config.settings import RiskSettings
from infrastructure.models import TradePlan
from infrastructure.workspace import fingerprint, now_iso, read_json, utc_today, write_json
from layers.risk.checker import CheckContext, RiskChecker
from layers.risk.state import RiskState


def risk_settings_from(cfg: dict[str, Any], equity: float) -> RiskSettings:
    rs = RiskSettings()
    per_trade = cfg["max_loss_per_trade_usd"] / equity if equity > 0 else 0.0
    if cfg.get("risk_per_trade_pct"):
        per_trade = min(per_trade, float(cfg["risk_per_trade_pct"]))
    return replace(rs, risk_per_trade_pct=per_trade,
                   max_daily_loss_pct=cfg["max_daily_loss_usd"] / equity if equity > 0 else 0.0,
                   max_leverage=float(cfg["max_leverage"]),
                   max_open_positions=int(cfg["max_open_positions"]),
                   max_symbol_exposure_x=float(cfg["max_symbol_exposure_x"]),
                   max_total_exposure_x=float(cfg["max_total_exposure_x"]),
                   min_rr=float(cfg["min_reward_risk"]), max_spread_pct=float(cfg["max_spread_pct"]),
                   max_abs_funding_rate=float(cfg["max_abs_funding_rate"]),
                   stale_data_seconds=float(cfg["stale_quote_seconds"]))


def check_orders(orders: list[dict[str, Any]], cfg: dict[str, Any], registry: Any, store: Any,
                 equity: float, day_start_equity: float, positions: dict[str, Any],
                 activity: dict[str, Any], risk_state: RiskState | None = None,
                 write: bool = True) -> dict[str, Any]:
    rs_cfg = risk_settings_from(cfg, equity)
    checker = RiskChecker(rs_cfg, CostModel())
    kill, why = risk_state.kill_switch_active() if risk_state else (False, "")
    open_count = sum(1 for p in positions.values() if p.is_open)
    per_sym: dict[str, float] = {}
    for sym, p in positions.items():
        if p.is_open:
            t = store.tickers.get(sym)
            per_sym[sym] = abs(registry.get(sym).notional(abs(p.size), (t.mark or t.last) if t else p.entry_price))
    total = sum(per_sym.values())
    trades_today = activity.get("entry_fills", 0)
    orders_today = activity.get("orders_placed", 0)
    results = []
    for idx, o in enumerate(orders, 1):
        reasons: list[str] = []
        warnings: list[str] = []
        sym = o.get("symbol", "")
        try:
            spec = registry.get(sym)
        except KeyError:
            results.append({"index": idx, "symbol": sym, "verdict": "BLOCK",
                            "reasons": [f"unknown instrument {sym}"], "fingerprint": fingerprint(o)})
            continue
        side, size = o.get("side"), o.get("size")
        if side not in ("buy", "sell"):
            reasons.append(f"bad side {side!r}")
        if not isinstance(size, int) or size < 1:
            reasons.append(f"size must be a whole number of contracts ≥ 1, got {size!r}")
        for k in ("limit_price", "stop_loss", "take_profit"):
            v = o.get(k)
            if v not in (None, ""):
                q = float(v) / spec.tick_size
                if abs(q - round(q)) > 1e-6:
                    reasons.append(f"{k} {v} is not a multiple of the tick {spec.tick_size}")
        reduce_only = bool(o.get("reduce_only"))
        pos = positions.get(sym)
        if reduce_only:
            if pos is None or not pos.is_open:
                reasons.append("reduce-only order but there is no position to reduce")
        else:
            if sym in {s.upper() for s in cfg.get("blocked", [])}:
                reasons.append(f"{sym} is blocked in config")
            allowed = {s.upper() for s in cfg.get("allowed_symbols", [])}
            if allowed and sym not in allowed:
                reasons.append(f"{sym} is not in allowed_symbols")
            if spec.is_option and not cfg.get("allow_options"):
                reasons.append("options trading is disabled (allow_options=false)")
            if side == "sell" and not cfg.get("allow_short", True):
                reasons.append("short selling disabled (allow_short=false)")
            if cfg.get("require_stop") and not o.get("stop_loss"):
                reasons.append("no stop-loss (require_stop=true)")
            if trades_today >= cfg["max_trades_per_day"]:
                reasons.append(f"max_trades_per_day {cfg['max_trades_per_day']} reached")
            if orders_today >= cfg["max_orders_per_day"]:
                reasons.append(f"max_orders_per_day {cfg['max_orders_per_day']} reached")
            if activity.get("realised_net", 0) <= -cfg["max_daily_loss_usd"]:
                reasons.append(f"daily loss ${-activity['realised_net']:.2f} ≥ limit "
                               f"${cfg['max_daily_loss_usd']:.2f} (UTC day)")
            t = store.tickers.get(sym)
            if t is None:
                reasons.append("no fresh quote for the symbol (stale/missing data blocks entries)")
            elif not reasons:
                entry = float(o.get("limit_price") or (t.mark or t.last))
                plan = TradePlan(sym, side, int(size), entry, float(o["stop_loss"]),
                                 float(o["take_profit"]) if o.get("take_profit") else None,
                                 float(o.get("leverage") or cfg["default_leverage"]), "desk",
                                 order_type=o.get("order_type", "market_order"),
                                 limit_price=float(o["limit_price"]) if o.get("limit_price") else None)
                ex_size = pos.size if pos else 0
                ex_upnl = pos.unrealised_pnl(spec, t.mark or t.last) if pos and pos.is_open else 0.0
                ctx = CheckContext(
                    equity=equity, day_start_equity=day_start_equity or equity, last_price=t.last,
                    mark_price=t.mark, funding_rate=t.funding_rate, spread_pct=t.spread_pct,
                    data_age_s=store.age(sym, "ticker"), open_position_count=open_count,
                    symbol_exposure=per_sym.get(sym, 0.0), total_exposure=total,
                    existing_size=ex_size, existing_upnl=ex_upnl, kill_switch=kill,
                    kill_reason=why, cooldown=risk_state.in_cooldown() if risk_state else False,
                    loss_streak=risk_state.loss_streak if risk_state else 0)
                res = checker.check(plan, spec, ctx)
                reasons += [f"{v.rule_id} {v.description}: {v.details}" for v in res.vetoes]
        verdict = "PASS" if not reasons else "BLOCK"
        if verdict == "PASS" and not reduce_only:      # as if it filled
            n = spec.notional(int(size), float(o.get("limit_price") or o["plan"]["entry"]))
            per_sym[sym] = per_sym.get(sym, 0.0) + n
            total += n
            open_count += 0 if (pos and pos.is_open) else 1
            trades_today += 1
            orders_today += 1
        results.append({"index": idx, "symbol": sym, "side": side, "size": size,
                        "verdict": verdict, "reasons": reasons, "warnings": warnings,
                        "fingerprint": fingerprint(o)})
    doc = {"checked_at": now_iso(), "day": utc_today(), "equity": round(equity, 6),
           "results": results}
    if write:
        state = read_json("state/risk.json", {"checks": []}) or {"checks": []}
        for r in results:
            state["checks"].append({"fingerprint": r["fingerprint"], "verdict": r["verdict"],
                                    "reasons": r["reasons"], "day": utc_today(), "at": now_iso()})
        state["checks"] = state["checks"][-2000:]
        write_json("state/risk.json", state)
    return doc


def latest_verdict(fp: str, day: str | None = None) -> dict[str, Any] | None:
    day = day or utc_today()
    for c in reversed((read_json("state/risk.json", {}) or {}).get("checks", [])):
        if c.get("fingerprint") == fp and c.get("day") == day:
            return c
    return None
