"""
Market core for the desk (crypto replacement of trader-market):

  clock   — 24/7 market: UTC + IST time, the next funding time per symbol, UTC-day
            risk reset countdown. There are no sessions, holidays or square-off.
  costs   — full round-trip cost of a trade on Delta India: maker/taker fee on
            notional + 18 % GST, funding for the holding period, slippage, the
            break-even move, margin and the estimated liquidation price.
  info    — contract specs for symbols (tick, contract value, margins, limits, fees).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from engines.costs import CostModel
from infrastructure.models import ContractSpec, Side
from layers.risk.sizing import liquidation_price, required_margin

IST = timezone(timedelta(hours=5, minutes=30))


def next_funding(now: datetime, hours_utc: list[int], next_ts: float | None = None) -> datetime:
    """Exchange-provided next funding time when known; else the configured UTC schedule."""
    if next_ts:
        return datetime.fromtimestamp(next_ts, tz=timezone.utc)
    for d in range(0, 2):
        day = (now + timedelta(days=d)).date()
        for h in sorted(hours_utc):
            t = datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc)
            if t > now:
                return t
    return now + timedelta(hours=8)


def clock(now: datetime | None = None, symbols: list[str] | None = None,
          tickers: dict[str, dict[str, Any]] | None = None,
          hours_utc: list[int] | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    hours_utc = hours_utc or [0, 8, 16]
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    out: dict[str, Any] = {
        "utc": now.isoformat(timespec="seconds"),
        "ist": now.astimezone(IST).isoformat(timespec="seconds"),
        "market": "open 24/7 (crypto derivatives; exchange maintenance is announced on the "
                  "system_status channel)",
        "risk_day_resets_in_min": round((midnight - now).total_seconds() / 60, 1),
        "funding": {},
    }
    for sym in symbols or []:
        t = (tickers or {}).get(sym) or {}
        nf = next_funding(now, hours_utc, t.get("next_funding_ts"))
        out["funding"][sym] = {
            "next_utc": nf.isoformat(timespec="minutes"),
            "in_min": round((nf - now).total_seconds() / 60, 1),
            "rate_pct": t.get("funding_rate_pct"),
            "source": "exchange" if t.get("next_funding_ts") else "schedule (estimate)",
        }
    return out


def costs(spec: ContractSpec, side: Side, entry: float, exit_: float, contracts: int,
          leverage: float = 5.0, maker_entry: bool = False, maker_exit: bool = False,
          hold_hours: float = 0.0, funding_rate: float = 0.0, slippage_bps: float = 0.0,
          cm: CostModel | None = None) -> dict[str, Any]:
    cm = cm or CostModel()
    slip = slippage_bps / 10_000
    sign = 1 if side == "buy" else -1
    fill_in = entry * (1 + sign * slip) if not maker_entry else entry
    fill_out = exit_ * (1 - sign * slip) if not maker_exit else exit_
    n_in, n_out = spec.notional(contracts, fill_in), spec.notional(contracts, fill_out)
    fee_in = cm.fee(spec, n_in, maker_entry)
    fee_out = cm.fee(spec, n_out, maker_exit)
    intervals = hold_hours * 3600 / max(spec.funding_interval_s, 1) if spec.is_perpetual else 0
    funding = cm.funding_payment(side, n_in, funding_rate) * intervals
    gross = spec.pnl(side, contracts, fill_in, fill_out)
    net = gross - fee_in - fee_out - funding
    rt_rate = (spec.maker_fee if maker_entry else spec.taker_fee) + \
        (spec.maker_fee if maker_exit else spec.taker_fee)
    breakeven_pct = (rt_rate * (1 + cm.gst_rate) + 2 * slip) * 100 + \
        (funding_rate * intervals * 100 * (1 if side == "buy" else -1))
    return {
        "symbol": spec.symbol, "side": side, "contracts": contracts,
        "underlying_qty": round(contracts * spec.contract_value, 8),
        "entry_fill": round(fill_in, 6), "exit_fill": round(fill_out, 6),
        "notional_in": round(n_in, 4), "leverage": leverage,
        "margin": round(required_margin(spec, contracts, fill_in, leverage), 4),
        "liquidation_price_est": round(liquidation_price(side, fill_in, leverage,
                                                         spec.maintenance_margin), 4),
        "fee_in": round(fee_in, 6), "fee_out": round(fee_out, 6),
        "gst_included": cm.gst_rate, "funding": round(funding, 6),
        "funding_intervals": round(intervals, 2),
        "gross_pnl": round(gross, 6), "net_pnl": round(net, 6),
        "breakeven_move_pct": round(breakeven_pct, 4),
    }


def info_rows(specs: list[ContractSpec]) -> list[dict[str, Any]]:
    return [{
        "symbol": s.symbol, "type": s.contract_type, "product_id": s.product_id,
        "tick": s.tick_size, "contract_value": s.contract_value, "underlying": s.underlying,
        "settling": s.settling_asset, "max_leverage": s.max_leverage,
        "initial_margin_pct": round(s.initial_margin * 100, 4),
        "maintenance_margin_pct": round(s.maintenance_margin * 100, 4),
        "maker_fee_pct": round(s.maker_fee * 100, 4), "taker_fee_pct": round(s.taker_fee * 100, 4),
        "position_size_limit": s.position_size_limit, "state": s.state,
        "funding_interval_h": round(s.funding_interval_s / 3600, 2) if s.is_perpetual else None,
    } for s in specs]
