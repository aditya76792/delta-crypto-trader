"""
Bar-replay backtester for crypto perpetuals.

Execution model (deliberately conservative):
  * signals are evaluated on CLOSED bar i; entries fill at bar i+1 OPEN ± slippage
    as taker orders (fee × (1 + 18 % GST))
  * protective stop and take-profit are checked intrabar on every later bar; if
    both could have triggered in the same bar the STOP is assumed first
  * gaps through the stop fill at the bar open (worse than the stop)
  * estimated liquidation is checked before the stop; liquidation loses the margin
  * funding is charged on open notional at every funding timestamp (UTC 00/08/16
    by default) at a configurable constant or per-bar rate series
  * risk rules (CR-xx) are applied exactly as in paper/live via RiskChecker
  * 24/7: no sessions, no EOD square-off — positions exit by SL/TP/strategy/time stop
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from engines.costs import CostModel
from infrastructure.config.settings import RiskSettings
from infrastructure.models import ContractSpec, side_sign
from layers.planning import build_plan
from layers.risk.checker import CheckContext, RiskChecker
from layers.risk.sizing import liquidation_price
from layers.technical.indicators import atr as atr_ind
from strategies.base import BaseStrategy

LOOKBACK_BARS = 1000   # strategies see a trailing window (same as live store)


@dataclass
class BTTrade:
    symbol: str
    side: str
    contracts: int
    entry_time: pd.Timestamp
    entry_price: float
    stop: float
    target: float | None
    leverage: float
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    risk_amount: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.risk_amount if self.risk_amount > 0 else 0.0


@dataclass
class BacktestResult:
    strategy_id: str
    symbol: str
    trades: list[BTTrade]
    equity_curve: pd.Series
    starting_equity: float
    vetoes: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def metrics(self, bars_per_year: float = 105_120) -> dict[str, Any]:
        eq = self.equity_curve
        n = len(self.trades)
        pnl = np.array([t.net_pnl for t in self.trades]) if n else np.array([])
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        rets = eq.pct_change().dropna()
        dd = (eq / eq.cummax() - 1).min() if len(eq) else 0.0
        sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) \
            if len(rets) > 2 and rets.std() > 0 else 0.0
        pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 \
            else (float("inf") if len(wins) else 0.0)
        return {
            "strategy": self.strategy_id, "symbol": self.symbol, "trades": n,
            "net_pnl": round(float(pnl.sum()), 4) if n else 0.0,
            "return_pct": round(float(eq.iloc[-1] / self.starting_equity - 1) * 100, 3) if len(eq) else 0.0,
            "max_drawdown_pct": round(float(dd) * 100, 3),
            "sharpe": round(sharpe, 3),
            "win_rate": round(len(wins) / n, 3) if n else 0.0,
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "expectancy_r": round(float(np.mean([t.r_multiple for t in self.trades])), 3) if n else 0,
            "exit_reasons": _count(t.exit_reason for t in self.trades),
            "fees": round(float(sum(t.fees for t in self.trades)), 4),
            "funding": round(float(sum(t.funding for t in self.trades)), 4),
            "liquidations": sum(1 for t in self.trades if t.exit_reason == "liquidation"),
            "vetoes": self.vetoes,
        }


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


class Backtester:
    def __init__(self, strategy: BaseStrategy, spec: ContractSpec, risk: RiskSettings,
                 starting_equity: float = 1000.0, leverage: float = 5.0,
                 slippage_bps: float = 3.0, funding_rate: float | pd.Series = 0.0001,
                 funding_hours: tuple[int, ...] = (0, 8, 16), costs: CostModel | None = None,
                 spread_pct: float = 0.0002) -> None:
        self.strategy = strategy
        self.spec = spec
        self.risk = risk
        self.equity0 = starting_equity
        self.leverage = leverage
        self.slip = slippage_bps / 10_000
        self.funding_rate = funding_rate
        self.funding_hours = set(funding_hours)
        self.costs = costs or CostModel()
        self.checker = RiskChecker(risk, self.costs)
        self.spread_pct = spread_pct

    def _funding_at(self, ts: pd.Timestamp) -> float:
        if isinstance(self.funding_rate, pd.Series):
            v = self.funding_rate.asof(ts)
            return 0.0 if pd.isna(v) else float(v)
        return float(self.funding_rate)

    def run(self, bars: pd.DataFrame, symbol: str | None = None) -> BacktestResult:
        symbol = symbol or self.spec.symbol
        spec = self.spec
        self.strategy.reset(symbol)
        bars = bars.sort_index()
        atr_series = atr_ind(bars, 14)
        o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
        idx = bars.index
        cash = self.equity0                # realised equity
        trades: list[BTTrade] = []
        curve = np.empty(len(bars))
        pos: BTTrade | None = None
        pending = None                     # plan to execute at next open
        entry_bar = 0
        vetoes: dict[str, int] = {}
        day = None
        day_start = cash
        warm = self.strategy.required_warmup_bars

        for i in range(len(bars)):
            ts = idx[i]
            # ── UTC day roll for daily-loss rule
            if day != ts.date():
                day = ts.date()
                day_start = cash + (self._upnl(pos, c[i - 1]) if pos and i else 0.0)

            # ── execute pending entry at this bar's open
            if pending is not None and pos is None:
                plan = pending
                pending = None
                side = plan.side
                fill = o[i] * (1 + self.slip * side_sign(side))
                fill = spec.round_price(fill)
                # re-validate the stop against the actual fill (gap protection)
                if (side == "buy" and plan.stop_loss < fill) or (side == "sell" and plan.stop_loss > fill):
                    fee = self.costs.fee(spec, spec.notional(plan.contracts, fill), is_maker=False)
                    pos = BTTrade(symbol, side, plan.contracts, ts, fill, plan.stop_loss,
                                  plan.take_profit, plan.leverage, fees=fee,
                                  risk_amount=spec.contract_value * plan.contracts *
                                  abs(fill - plan.stop_loss) + 2 * fee)
                    entry_bar = i

            # ── manage open position intrabar
            if pos is not None:
                exit_px, reason = self._intrabar_exit(pos, o[i], h[i], l[i])
                if exit_px is None and i > entry_bar:
                    window = bars.iloc[max(0, i - LOOKBACK_BARS):i + 1]
                    ex = self.strategy.should_exit(symbol, window, pos.side, pos.entry_price,
                                                   i - entry_bar)
                    if ex is not None:
                        exit_px = c[i] * (1 - self.slip * side_sign(pos.side))
                        reason = ex.metadata.get("reason", "strategy_exit")
                if exit_px is None and ts.hour in self.funding_hours and ts.minute == 0 \
                        and i > entry_bar:
                    pos.funding += self.costs.funding_payment(
                        pos.side, spec.notional(pos.contracts, o[i]), self._funding_at(ts))
                if exit_px is not None:
                    cash += self._close(pos, ts, exit_px, reason)
                    trades.append(pos)
                    pos = None

            equity = cash + (self._upnl(pos, c[i]) if pos else 0.0)
            curve[i] = equity

            # ── new signal on CLOSED bar i (fills next bar)
            if pos is None and pending is None and i >= warm and i < len(bars) - 1:
                window = bars.iloc[max(0, i - LOOKBACK_BARS):i + 1]
                sig = self.strategy.on_bar(symbol, window)
                if sig.is_actionable:
                    pr = build_plan(sig, spec, float(c[i]), equity, equity, self.leverage,
                                    self.risk, self.strategy.strategy_id, costs=self.costs)
                    if pr.plan is None:
                        vetoes[pr.reason] = vetoes.get(pr.reason, 0) + 1
                        continue
                    ctx = CheckContext(equity=equity, day_start_equity=day_start,
                                       last_price=float(c[i]), mark_price=float(c[i]),
                                       funding_rate=self._funding_at(ts),
                                       spread_pct=self.spread_pct, data_age_s=0.0,
                                       atr=float(atr_series.iloc[i]))
                    chk = self.checker.check(pr.plan, spec, ctx)
                    if chk.approved:
                        pending = pr.plan
                    else:
                        for v in chk.veto_ids:
                            vetoes[v] = vetoes.get(v, 0) + 1

        if pos is not None:   # mark-to-market close at the end of data
            cash += self._close(pos, idx[-1], float(c[-1]), "end_of_data")
            trades.append(pos)
            curve[-1] = cash
        return BacktestResult(self.strategy.strategy_id, symbol, trades,
                              pd.Series(curve, index=idx), self.equity0, vetoes,
                              dict(self.strategy.params))

    # ── helpers ─────────────────────────────────────────────────────────────

    def _upnl(self, pos: BTTrade | None, price: float) -> float:
        if pos is None:
            return 0.0
        return self.spec.pnl(pos.side, pos.contracts, pos.entry_price, price) - pos.fees - pos.funding

    def _intrabar_exit(self, pos: BTTrade, o: float, h: float, low: float
                       ) -> tuple[float | None, str]:
        liq = liquidation_price(pos.side, pos.entry_price, pos.leverage,
                                self.spec.maintenance_margin)
        if pos.side == "buy":
            if o <= pos.stop:                       # gapped through stop
                return (o if o > liq else liq), ("liquidation" if o <= liq else "stop_gap")
            if low <= pos.stop:
                return pos.stop * (1 - self.slip), "stop"
            if pos.target is not None and h >= pos.target:
                return pos.target, "target"
        else:
            if o >= pos.stop:
                return (o if o < liq else liq), ("liquidation" if o >= liq else "stop_gap")
            if h >= pos.stop:
                return pos.stop * (1 + self.slip), "stop"
            if pos.target is not None and low <= pos.target:
                return pos.target, "target"
        return None, ""

    def _close(self, pos: BTTrade, ts: pd.Timestamp, price: float, reason: str) -> float:
        spec = self.spec
        pos.exit_time, pos.exit_price, pos.exit_reason = ts, price, reason
        maker = reason == "target"   # TP rests as a limit order on the book
        pos.fees += self.costs.fee(spec, spec.notional(pos.contracts, price), is_maker=maker)
        if reason == "liquidation":
            margin = spec.notional(pos.contracts, pos.entry_price) / pos.leverage
            pos.gross_pnl = -margin
        else:
            pos.gross_pnl = spec.pnl(pos.side, pos.contracts, pos.entry_price, price)
        return pos.net_pnl
