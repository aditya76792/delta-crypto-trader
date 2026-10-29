"""
RiskChecker: applies every crypto rule (CR-01 … CR-20) to a TradePlan.

Same shape as the equity checker (CheckContext → CheckResult with veto list), but
the context now carries crypto state: mark/index prices, funding, spread, data
age, exposure in quote currency, leverage, ATR and kill-switch status.
Missing market data is treated as a FAIL (stale-data protection), not a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engines.costs import CostModel
from infrastructure.config.settings import RiskSettings
from infrastructure.models import ContractSpec, TradePlan
from layers.risk import rules as R
from layers.risk.rules import RuleResult


@dataclass
class CheckContext:
    equity: float
    day_start_equity: float
    last_price: float
    mark_price: float | None = None
    funding_rate: float | None = None
    spread_pct: float | None = None
    data_age_s: float = float("inf")
    atr: float | None = None
    open_position_count: int = 0
    symbol_exposure: float = 0.0
    total_exposure: float = 0.0
    existing_size: int = 0
    existing_upnl: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""
    cooldown: bool = False
    loss_streak: int = 0
    orders_last_minute: int = 0
    reduce_only: bool = False


@dataclass
class CheckResult:
    approved: bool
    plan_id: str
    vetoes: list[RuleResult] = field(default_factory=list)
    results: list[RuleResult] = field(default_factory=list)

    @property
    def veto_ids(self) -> list[str]:
        return [v.rule_id for v in self.vetoes]

    def summary(self) -> str:
        return ", ".join(f"{v.rule_id}:{v.description}" for v in self.vetoes) or "approved"


class RiskChecker:
    def __init__(self, cfg: RiskSettings, costs: CostModel | None = None) -> None:
        self.cfg = cfg
        self.costs = costs or CostModel()

    def check(self, plan: TradePlan, spec: ContractSpec, ctx: CheckContext) -> CheckResult:
        c = self.cfg
        res: list[RuleResult] = []
        add = res.append

        # Hard stops first — nothing else matters if these fail
        add(R.cr18_kill_switch(ctx.kill_switch, ctx.kill_reason))
        add(R.cr04_daily_loss(ctx.equity - ctx.day_start_equity, ctx.day_start_equity,
                              c.max_daily_loss_pct))
        add(R.cr12_fresh_data(ctx.data_age_s, c.stale_data_seconds))
        add(R.cr15_instrument_tradable(spec))

        if not ctx.reduce_only:
            notional = spec.notional(plan.contracts, plan.entry_price)
            add(R.cr01_stop_defined(plan))
            add(R.cr02_size_valid(plan, spec))
            add(R.cr03_trade_risk(plan, spec, ctx.equity, c.risk_per_trade_pct,
                                  self.costs.round_trip_fee_rate(spec)))
            add(R.cr05_max_positions(ctx.open_position_count, c.max_open_positions,
                                     adds_new=ctx.existing_size == 0))
            add(R.cr06_symbol_exposure(notional, ctx.symbol_exposure, ctx.equity,
                                       c.max_symbol_exposure_x))
            add(R.cr07_total_exposure(notional, ctx.total_exposure, ctx.equity,
                                      c.max_total_exposure_x))
            add(R.cr08_leverage(plan, spec, c.max_leverage))
            add(R.cr09_liquidation_distance(plan, spec, ctx.atr, c.min_liq_distance_atr,
                                            c.min_liq_buffer_pct))
            add(R.cr10_min_rr(plan, c.min_rr))
            add(R.cr11_spread(ctx.spread_pct, c.max_spread_pct))
            add(R.cr13_mark_divergence(ctx.last_price, ctx.mark_price,
                                       c.max_mark_last_divergence_pct))
            add(R.cr14_funding(plan, spec, ctx.funding_rate, c.max_abs_funding_rate))
            add(R.cr16_price_on_tick(plan, spec))
            add(R.cr17_no_averaging_down(plan, ctx.existing_size, ctx.existing_upnl))
            add(R.cr19_loss_streak(ctx.cooldown, ctx.loss_streak))
            add(R.cr20_order_rate(ctx.orders_last_minute, c.max_orders_per_minute))

        vetoes = [r for r in res if not r.passed]
        if ctx.reduce_only:
            # Reducing / closing risk is never blocked (kill switch, stale data and
            # daily-loss breaches must still be able to flatten positions).
            vetoes = []
        plan.status = "APPROVED" if not vetoes else "VETOED"
        return CheckResult(not vetoes, plan.plan_id, vetoes, res)
