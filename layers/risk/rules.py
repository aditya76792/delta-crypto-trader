"""
Crypto risk rules (CR-xx). Each rule is a pure function returning RuleResult.

Equity-only rules removed (and why):
  RR-05 sector exposure, RR-13 market cap, RR-14 circuit limits, RR-09 earnings
  blackout, RR-10 NIFTY regime, RR-15/16 session open/close windows — crypto has
  no sectors/circuits/sessions; they are replaced by funding, liquidation,
  leverage, spread, stale-data and mark/last divergence checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from infrastructure.models import ContractSpec, TradePlan
from layers.risk.sizing import liquidation_price


@dataclass
class RuleResult:
    rule_id: str
    passed: bool
    description: str
    details: dict[str, Any] = field(default_factory=dict)


def _r(rule_id: str, ok: bool, desc: str, **details: Any) -> RuleResult:
    return RuleResult(rule_id, bool(ok), desc, details)


def cr01_stop_defined(plan: TradePlan) -> RuleResult:
    ok = plan.stop_loss > 0 and (
        plan.stop_loss < plan.entry_price if plan.side == "buy" else plan.stop_loss > plan.entry_price)
    return _r("CR-01", ok, "Stop-loss defined and on the protective side",
              side=plan.side, entry=plan.entry_price, stop=plan.stop_loss)


def cr02_size_valid(plan: TradePlan, spec: ContractSpec) -> RuleResult:
    ok = isinstance(plan.contracts, int) and spec.min_size <= plan.contracts <= spec.position_size_limit
    return _r("CR-02", ok, "Size is a whole number of contracts within exchange limits",
              contracts=plan.contracts, limit=spec.position_size_limit)


def cr03_trade_risk(plan: TradePlan, spec: ContractSpec, equity: float, max_pct: float,
                    fee_rate_rt: float) -> RuleResult:
    risk = plan.contracts * spec.contract_value * plan.risk_per_contract \
        + spec.notional(plan.contracts, plan.entry_price) * fee_rate_rt
    limit = equity * max_pct * 1.05   # 5 % tolerance for rounding
    return _r("CR-03", risk <= limit, "Per-trade risk (incl. fees) within budget",
              risk=round(risk, 4), limit=round(limit, 4))


def cr04_daily_loss(day_pnl: float, equity_start: float, max_loss_pct: float) -> RuleResult:
    pct = day_pnl / equity_start if equity_start > 0 else 0.0
    return _r("CR-04", pct > -max_loss_pct, "Daily loss limit not breached (UTC day)",
              day_pnl=round(day_pnl, 4), pct=round(pct, 5), limit=-max_loss_pct)


def cr05_max_positions(open_positions: int, max_positions: int, adds_new: bool) -> RuleResult:
    ok = (open_positions < max_positions) or not adds_new
    return _r("CR-05", ok, "Max concurrent positions", open=open_positions, max=max_positions)


def cr06_symbol_exposure(new_notional: float, existing: float, equity: float, cap_x: float
                         ) -> RuleResult:
    total = new_notional + existing
    return _r("CR-06", total <= equity * cap_x * 1.001, "Per-symbol notional exposure cap",
              notional=round(total, 2), cap=round(equity * cap_x, 2))


def cr07_total_exposure(new_notional: float, existing_total: float, equity: float,
                        cap_x: float) -> RuleResult:
    total = new_notional + existing_total
    return _r("CR-07", total <= equity * cap_x * 1.001, "Total notional exposure cap",
              notional=round(total, 2), cap=round(equity * cap_x, 2))


def cr08_leverage(plan: TradePlan, spec: ContractSpec, max_lev: float) -> RuleResult:
    ok = 1 <= plan.leverage <= min(max_lev, spec.max_leverage)
    return _r("CR-08", ok, "Leverage within configured and exchange limits",
              leverage=plan.leverage, cap=min(max_lev, spec.max_leverage))


def cr09_liquidation_distance(plan: TradePlan, spec: ContractSpec, atr: float | None,
                              min_atr: float, buffer_frac: float) -> RuleResult:
    """Stop must trigger well before liquidation: |entry-stop| <= buffer × |entry-liq|
    and liquidation must be >= min_atr ATRs beyond the stop."""
    liq = liquidation_price(plan.side, plan.entry_price, plan.leverage, spec.maintenance_margin)
    to_liq = abs(plan.entry_price - liq)
    to_stop = abs(plan.entry_price - plan.stop_loss)
    ok = to_stop <= buffer_frac * to_liq
    if atr and atr > 0:
        ok = ok and abs(liq - plan.stop_loss) >= min_atr * atr
    return _r("CR-09", ok, "Stop sits safely inside liquidation distance",
              liq=round(liq, 4), stop=plan.stop_loss, to_liq=round(to_liq, 4),
              to_stop=round(to_stop, 4))


def cr10_min_rr(plan: TradePlan, min_rr: float) -> RuleResult:
    rr = plan.rr_ratio
    ok = rr is None or rr >= min_rr
    return _r("CR-10", ok, "Reward:risk meets minimum", rr=rr and round(rr, 2), min=min_rr)


def cr11_spread(spread_pct: float | None, max_spread: float) -> RuleResult:
    ok = spread_pct is None or spread_pct <= max_spread
    return _r("CR-11", ok, "Bid/ask spread acceptable", spread=spread_pct, max=max_spread)


def cr12_fresh_data(age_s: float, max_age_s: float) -> RuleResult:
    return _r("CR-12", age_s <= max_age_s, "Market data is fresh",
              age_s=round(age_s, 2) if age_s != float("inf") else "missing", max=max_age_s)


def cr13_mark_divergence(last: float, mark: float | None, max_div: float) -> RuleResult:
    if not mark:
        return _r("CR-13", True, "Mark/last divergence (mark unavailable)")
    div = abs(last - mark) / mark
    return _r("CR-13", div <= max_div, "Last price close to mark price",
              divergence=round(div, 5), max=max_div)


def cr14_funding(plan: TradePlan, spec: ContractSpec, funding_rate: float | None,
                 max_abs: float) -> RuleResult:
    if not spec.is_perpetual or funding_rate is None:
        return _r("CR-14", True, "Funding not applicable")
    against = funding_rate if plan.side == "buy" else -funding_rate
    return _r("CR-14", against <= max_abs, "Funding rate not excessively against the position",
              funding_rate=funding_rate, against=against, max=max_abs)


def cr15_instrument_tradable(spec: ContractSpec) -> RuleResult:
    return _r("CR-15", spec.tradable, "Instrument is live/tradable", state=spec.state)


def cr16_price_on_tick(plan: TradePlan, spec: ContractSpec) -> RuleResult:
    def on_tick(p: float | None) -> bool:
        if p is None:
            return True
        q = p / spec.tick_size
        return abs(q - round(q)) < 1e-6
    ok = on_tick(plan.stop_loss) and on_tick(plan.take_profit) and on_tick(plan.limit_price)
    return _r("CR-16", ok, "Prices aligned to tick size", tick=spec.tick_size)


def cr17_no_averaging_down(plan: TradePlan, existing_size: int, existing_upnl: float) -> RuleResult:
    same_dir = (existing_size > 0 and plan.side == "buy") or (existing_size < 0 and plan.side == "sell")
    ok = not (same_dir and existing_upnl < 0)
    return _r("CR-17", ok, "No adding to a losing position", existing=existing_size,
              upnl=round(existing_upnl, 4))


def cr18_kill_switch(active: bool, reason: str = "") -> RuleResult:
    return _r("CR-18", not active, "Kill switch not engaged", reason=reason)


def cr19_loss_streak(cooldown_active: bool, streak: int) -> RuleResult:
    return _r("CR-19", not cooldown_active, "Not in loss-streak cooldown", streak=streak)


def cr20_order_rate(orders_last_minute: int, max_per_minute: int) -> RuleResult:
    return _r("CR-20", orders_last_minute < max_per_minute, "Order rate below throttle",
              orders_last_minute=orders_last_minute, max=max_per_minute)
