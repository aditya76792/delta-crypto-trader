"""
Exit planner (crypto rebuild of trader-exit): stop and target BEFORE entering.

Stops (rounded to the tick AWAY from the entry, so risk is never understated):
  range-mid | range-low (long) / range-high (short) | atr:K | pct:X | price:X |
  swing:N (lowest low / highest high of the last N closed bars, one tick beyond) | vwap
Targets (rounded TOWARD the entry, so reward is never overstated):
  2R (default), 1.5R … | price:X | pct:X
Crypto specifics instead of square-off times:
  * time stop: max_hold_hours (24/7 market — no forced flat time)
  * leverage, required margin and the estimated liquidation price; the plan is
    refused when the stop is not safely inside the liquidation distance
  * costs: taker entry (maker for a resting limit), taker stop, maker target,
    + 18 % GST, + funding for the expected hold at the current rate
Writes trader/plan.json.
"""

from __future__ import annotations

import math
from typing import Any

from engines.costs import CostModel
from infrastructure.models import ContractSpec
from infrastructure.workspace import now_iso, write_json
from layers.risk.sizing import liquidation_price, required_margin
from layers.technical import indicators as ta


class PlanError(ValueError):
    pass


def _away(spec: ContractSpec, price: float, long: bool) -> float:
    """Round a stop away from the entry: long stop down, short stop up."""
    t = spec.tick_size
    q = price / t
    return round((math.floor(q + 1e-9) if long else math.ceil(q - 1e-9)) * t, 10)


def parse_rule(text: str) -> tuple[str, float | None]:
    t = str(text).strip().lower()
    if t.endswith("r") and t[:-1].replace(".", "", 1).isdigit():
        return "r", float(t[:-1])
    if ":" in t:
        k, v = t.split(":", 1)
        return k, float(v)
    try:
        return "price", float(t)
    except ValueError:
        return t, None


def compute(spec: ContractSpec, side: str, entry: float, stop_rule: str = "atr:1.5",
            target_rule: str = "2R", bars: Any = None, rng: dict[str, float] | None = None,
            leverage: float = 5.0, max_hold_hours: float = 8.0, funding_rate: float = 0.0,
            ref_contracts: int | None = None, maker_entry: bool = False,
            liq_buffer: float = 0.5, cm: CostModel | None = None) -> dict[str, Any]:
    cm = cm or CostModel()
    long = side == "buy"
    k, v = parse_rule(stop_rule)
    if k in ("range-mid", "range-low", "range-high"):
        if not rng:
            raise PlanError(f"stop {stop_rule} needs the setup's range (from entry.json or --range)")
        raw = (rng["high"] + rng["low"]) / 2 if k == "range-mid" else (rng["low"] if long else rng["high"])
        if k == "range-high" and long or k == "range-low" and not long:
            raise PlanError(f"{k} is on the wrong side for a {side}")
    elif k == "atr":
        if bars is None or len(bars) < 20:
            raise PlanError("atr stop needs recent candles")
        a = float(ta.atr(bars, 14).iloc[-1])
        raw = entry - v * a if long else entry + v * a
    elif k == "pct":
        raw = entry * (1 - v / 100) if long else entry * (1 + v / 100)
    elif k == "price":
        raw = v
    elif k == "swing":
        if bars is None:
            raise PlanError("swing stop needs recent candles")
        n = int(v or 10)
        raw = float(bars["low"].iloc[-n:].min()) - spec.tick_size if long else \
            float(bars["high"].iloc[-n:].max()) + spec.tick_size
    elif k == "vwap":
        if bars is None:
            raise PlanError("vwap stop needs recent candles")
        vw = float(ta.rolling_vwap(bars, min(288, len(bars))).iloc[-1])
        raw = vw - spec.tick_size if long else vw + spec.tick_size
    else:
        raise PlanError(f"unknown stop rule {stop_rule!r}")
    stop = _away(spec, raw, long)
    if (long and stop >= entry) or (not long and stop <= entry):
        raise PlanError(f"stop {stop} is not on the protective side of entry {entry} for a {side}")
    risk = abs(entry - stop)

    tk, tv = parse_rule(target_rule)
    if tk == "r":
        traw = entry + tv * risk if long else entry - tv * risk
    elif tk == "pct":
        traw = entry * (1 + tv / 100) if long else entry * (1 - tv / 100)
    elif tk == "price":
        traw = tv
    else:
        raise PlanError(f"unknown target rule {target_rule!r}")
    target = _away(spec, traw, long)   # toward the entry: long target down, short target up
    if (long and target <= entry) or (not long and target >= entry):
        raise PlanError("target is not beyond the entry")
    reward = abs(target - entry)

    lev = max(1.0, min(leverage, spec.max_leverage))
    liq = liquidation_price(side, entry, lev, spec.maintenance_margin)
    to_liq = abs(entry - liq)
    if risk > liq_buffer * to_liq:
        raise PlanError(f"stop {stop} is too close to the estimated liquidation {liq:.4f} at "
                        f"{lev:g}x (stop uses {risk / to_liq:.0%} of the distance; max "
                        f"{liq_buffer:.0%}). Lower the leverage or tighten the stop.")

    n = ref_contracts or 1
    notional = spec.notional(n, entry)
    fee_in = cm.fee(spec, notional, maker_entry)
    fee_stop = cm.fee(spec, spec.notional(n, stop), False)
    fee_tgt = cm.fee(spec, spec.notional(n, target), True)
    intervals = max_hold_hours * 3600 / spec.funding_interval_s if spec.is_perpetual else 0.0
    fund = cm.funding_payment(side, notional, funding_rate) * intervals
    gross_r, gross_risk = spec.contract_value * n * reward, spec.contract_value * n * risk
    net_reward = gross_r - fee_in - fee_tgt - max(fund, 0)
    net_risk = gross_risk + fee_in + fee_stop + max(fund, 0)
    warnings = []
    if (fee_in + fee_tgt) > 0.25 * gross_r:
        warnings.append(f"fees eat {(fee_in + fee_tgt) / gross_r:.0%} of the reward")
    if fund > 0 and fund > 0.1 * gross_r:
        warnings.append(f"funding over {max_hold_hours:g}h costs {fund / gross_r:.0%} of the reward")
    return {
        "side": side, "entry": entry, "stop": stop, "stop_rule": stop_rule, "target": target,
        "target_rule": target_rule, "tick": spec.tick_size,
        "risk_per_contract": round(spec.contract_value * risk, 8),
        "reward_risk": round(reward / risk, 3),
        "net_reward_risk": round(net_reward / net_risk, 3) if net_risk > 0 else None,
        "ref_contracts": n, "costs": {"fee_in": round(fee_in, 6), "fee_stop": round(fee_stop, 6),
                                      "fee_target": round(fee_tgt, 6), "gst_rate": cm.gst_rate,
                                      "funding_est": round(fund, 6),
                                      "funding_rate": funding_rate},
        "leverage": lev, "liquidation_price_est": round(liq, 6),
        "stop_share_of_liq_distance": round(risk / to_liq, 3),
        "margin_ref": round(required_margin(spec, n, entry, lev), 6),
        "max_hold_hours": max_hold_hours, "warnings": warnings,
    }


def build_plan(spec: ContractSpec, entry_doc: dict[str, Any] | None, cfg: dict[str, Any],
               stop_rule: str | None = None, target_rule: str = "2R", bars: Any = None,
               entry: float | None = None, side: str | None = None,
               funding_rate: float = 0.0, leverage: float | None = None,
               write: bool = False) -> dict[str, Any]:
    e = entry_doc or {}
    side = side or e.get("side") or "buy"
    entry = float(entry if entry is not None else e.get("price"))
    rng = (e.get("context") or {}).get("range")
    if stop_rule is None:
        if e.get("suggested_stop"):
            stop_rule = f"price:{e['suggested_stop']}"
        elif rng:
            stop_rule = "range-mid"
        else:
            stop_rule = "atr:1.5"
    if e.get("suggested_target") and target_rule == "2R" and e.get("setup", "").startswith("strategy:"):
        target_rule = f"price:{e['suggested_target']}"
    # reference size: what the max loss allows at this risk (for realistic fee maths)
    lev = leverage or cfg["default_leverage"]
    lev = min(lev, cfg["max_leverage"])
    core = compute(spec, side, entry, stop_rule, target_rule, bars, rng, lev,
                   cfg["max_hold_hours"], funding_rate)
    per = core["risk_per_contract"] + spec.notional(1, entry) * CostModel().round_trip_fee_rate(spec)
    ref = max(1, int(cfg["max_loss_per_trade_usd"] // per)) if per > 0 else 1
    core = compute(spec, side, entry, stop_rule, target_rule, bars, rng, lev,
                   cfg["max_hold_hours"], funding_rate, ref_contracts=ref)
    plan = {"created_by": "desk-exit", "created_at": now_iso(), "symbol": spec.symbol,
            "product_id": spec.product_id, "contract_type": spec.contract_type,
            "setup": e.get("setup"), "resolution": e.get("resolution", cfg["resolution"]),
            "entry_bar_open_utc": e.get("last_bar_open_utc"), "range": rng, **core}
    if core["reward_risk"] < cfg["min_reward_risk"]:
        plan["warnings"].append(f"reward:risk {core['reward_risk']} < min {cfg['min_reward_risk']}")
    if write:
        write_json("plan.json", plan)
    return plan
