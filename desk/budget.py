"""
Budget manager (crypto rebuild of trader-budget): sizes ONE trade from
trader/plan.json in whole CONTRACTS so the loss at the stop INCLUDING fees + GST
(and funding for the expected hold) stays inside your limit, then writes one
order — entry plus exchange-side bracket stop/target — to trader/orders.json.
It never places anything: the risk check and your explicit yes come next.

contracts = min of
  risk budget     max_loss_per_trade_usd (or risk_per_trade_pct × equity)
                  / (risk per contract + round-trip fees + GST per contract)
  margin          available margin × leverage / notional per contract (5 % buffer)
  exposure caps   per-symbol and total notional caps (× equity)
  exchange limit  product position_size_limit
"""

from __future__ import annotations

from typing import Any

from engines.costs import CostModel
from infrastructure.models import ContractSpec, new_id
from infrastructure.workspace import now_iso, write_json
from layers.risk.sizing import liquidation_price, required_margin, size_position


class SizeError(ValueError):
    pass


def size_order(plan: dict[str, Any], spec: ContractSpec, cfg: dict[str, Any], equity: float,
               available: float, symbol_exposure: float = 0.0, total_exposure: float = 0.0,
               order_type: str = "market_order", limit_price: float | None = None,
               cm: CostModel | None = None) -> dict[str, Any]:
    cm = cm or CostModel()
    side, entry, stop, target = plan["side"], float(plan["entry"]), float(plan["stop"]), plan.get("target")
    if spec.is_option and not cfg.get("allow_options"):
        raise SizeError("options are analysis-only (set allow_options in trader/config.json)")
    if side == "sell" and not cfg.get("allow_short", True):
        raise SizeError("allow_short is false")
    lev = min(float(plan.get("leverage") or cfg["default_leverage"]), cfg["max_leverage"],
              spec.max_leverage)
    budget = cfg["max_loss_per_trade_usd"]
    if cfg.get("risk_per_trade_pct"):
        budget = min(budget, equity * float(cfg["risk_per_trade_pct"]))
    risk_pct = budget / equity if equity > 0 else 0.0
    ref = limit_price if (order_type == "limit_order" and limit_price) else entry
    sz = size_position(spec, side, equity, available, ref, stop, lev, risk_pct,
                       symbol_exposure, total_exposure, cfg["max_symbol_exposure_x"],
                       cfg["max_total_exposure_x"], cm)
    n = sz.contracts
    if n < 1:
        raise SizeError(f"one contract already breaks a limit ({sz.binding_constraint}); "
                        f"max loss ${budget:.2f} vs ${sz.risk_amount or 0:.2f}")
    notional = spec.notional(n, ref)
    loss_at_stop = spec.contract_value * n * abs(ref - stop) + \
        cm.fee(spec, notional, order_type == "limit_order") + cm.fee(spec, spec.notional(n, stop), False)
    gain_at_target = (spec.contract_value * n * abs(float(target) - ref)
                      - cm.fee(spec, notional, order_type == "limit_order")
                      - cm.fee(spec, spec.notional(n, float(target)), True)) if target else None
    coid = new_id("dct")[:32]
    body: dict[str, Any] = {
        "product_id": spec.product_id, "product_symbol": spec.symbol, "size": n, "side": side,
        "order_type": order_type, "client_order_id": coid, "time_in_force": "gtc",
        "reduce_only": False, "bracket_stop_trigger_method": "mark_price",
        "bracket_stop_loss_price": str(stop),
    }
    if target:
        body["bracket_take_profit_price"] = str(target)
    if order_type == "limit_order":
        body["limit_price"] = str(spec.round_price(limit_price or entry, side))
    return {
        "symbol": spec.symbol, "product_id": spec.product_id, "side": side, "size": n,
        "order_type": order_type, "limit_price": body.get("limit_price"),
        "leverage": lev, "stop_loss": stop, "take_profit": target, "reduce_only": False,
        "client_order_id": coid, "delta_body": body,
        "plan": {"entry": entry, "stop": stop, "target": target, "setup": plan.get("setup"),
                 "max_hold_hours": plan.get("max_hold_hours"),
                 "risk_per_contract": round(spec.contract_value * abs(ref - stop), 8),
                 "max_loss_limit_usd": round(budget, 4)},
        "costs": {"loss_at_stop_with_fees": round(loss_at_stop, 6),
                  "net_gain_at_target": round(gain_at_target, 6) if gain_at_target is not None else None,
                  "gst_rate": cm.gst_rate},
        "margin": {"required": round(required_margin(spec, n, ref, lev), 6), "leverage": lev,
                   "notional": round(notional, 6)},
        "liquidation_price_est": round(liquidation_price(side, ref, lev, spec.maintenance_margin), 6),
        "binding_constraint": sz.binding_constraint,
        "why": (f"{n} contracts × {spec.contract_value:g} {spec.underlying or 'units'}: loss at the stop "
                f"incl. fees+GST ${loss_at_stop:.2f} ≤ ${budget:.2f} (bound by {sz.binding_constraint})"),
    }


def write_orders(orders: list[dict[str, Any]], paper: bool, source: str) -> dict[str, Any]:
    doc = {"created_by": "desk-budget", "created_at": now_iso(), "status": "proposed",
           "paper": paper, "source": source,
           "note": "Proposals only. Nothing is sent until the risk check passes THIS order "
                   "and you approve it with an explicit yes.",
           "orders": orders}
    write_json("orders.json", doc)
    return doc
