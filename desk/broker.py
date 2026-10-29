"""
Order executor (crypto rebuild of trader-broker). Nothing is ever sent without
your explicit yes for THAT order.

  show      each proposed order as a ticket in USD: contracts and underlying qty,
            leverage, margin, liquidation estimate, bracket stop/target, the loss at
            the stop INCLUDING fees + GST, and the exact Delta /v2/orders body.
            READY only if the risk check passed THIS exact order (fingerprint) today
            and nobody has decided on it yet.
  approve   records your yes for ONE order, word for word. Refuses "ok", "sure",
            "go", "yes to all", a yes with conditions, or one that names another
            symbol / a different size.
  decline   records a no.
  place     sends ONE approved order: the paper exchange by default; live only when
            TRADING_MODE=live AND the live approval process is complete (see LIVE.md).
  flatten   builds a reduce-only market ticket that closes a position (it still goes
            through risk + your yes).
  log       today's decisions.
Everything is appended to trader/state/broker.json.
"""

from __future__ import annotations

import re
from typing import Any

from desk.risk_check import latest_verdict
from infrastructure.models import new_id
from infrastructure.workspace import fingerprint, now_iso, read_json, utc_today, write_json

YES_WORDS = {"yes", "y", "yep", "yeah"}
BATCH_WORDS = {"all", "every", "both", "everything", "batch"}
DOUBT_WORDS = {"but", "if", "unless", "maybe", "perhaps", "not", "no", "wait", "only", "except"}
FILLER = {"yes", "y", "yep", "yeah", "place", "it", "send", "the", "order", "please", "go",
          "ahead", "confirm", "confirmed", "for", "contracts", "contract", "lots", "x", "at",
          "market", "limit", "perp", "perpetual", "usd", "this", "one", "trade"}


class BrokerError(RuntimeError):
    pass


def load_state() -> dict[str, Any]:
    return read_json("state/broker.json", {"log": []}) or {"log": []}


def record(event: str, order: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    st = load_state()
    rec = {"event": event, "at": now_iso(), "day": utc_today(), **extra}
    if order is not None:
        rec.update({"fingerprint": fingerprint(order), "symbol": order.get("symbol"),
                    "side": order.get("side"), "size": order.get("size"),
                    "client_order_id": order.get("client_order_id")})
    st["log"].append(rec)
    write_json("state/broker.json", st)
    return rec


def decision(fp: str, day: str | None = None) -> dict[str, Any] | None:
    day = day or utc_today()
    for r in reversed(load_state()["log"]):
        if r.get("fingerprint") == fp and r.get("day") == day and r.get("event") in (
                "approved", "declined", "placed", "failed"):
            return r
    return None


def status_of(order: dict[str, Any]) -> tuple[str, str]:
    fp = fingerprint(order)
    rv = latest_verdict(fp)
    if rv is None:
        return "NOT CHECKED", "run the risk check on this exact order first"
    if rv["verdict"] != "PASS":
        return "BLOCKED", "; ".join(rv["reasons"])[:300]
    d = decision(fp)
    if d is None:
        return "READY", "risk PASS today; waiting for your explicit yes"
    return d["event"].upper(), f"{d['event']} at {d['at']}"


def plain(order: dict[str, Any]) -> str:
    return (f"{order['side'].upper()} {order['size']} {order['symbol']} "
            f"{'@ ' + str(order['limit_price']) if order.get('limit_price') else 'at market'}")


def parse_yes(reply: str, order: dict[str, Any], batch_symbols: tuple[str, ...] = ()) -> tuple[bool, str]:
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, 'only a reply that starts with "yes" counts (not ok, sure, go, do it)'
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = order["symbol"].upper()
    upper = raw.upper()
    others = {s.upper() for s in batch_symbols} - {sym}
    named = {s for s in others if re.search(rf"(?<![A-Z0-9]){re.escape(s)}(?![A-Z0-9])", upper)}
    if named:
        return False, f"the reply names {', '.join(sorted(named))}, but this order is {sym}"
    rest = re.sub(rf"(?<![A-Z0-9]){re.escape(sym)}(?![A-Z0-9])", " ", upper)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", rest)]
    want = {float(order["size"])}
    if order.get("limit_price"):
        want.add(float(order["limit_price"]))
    bad = [n for n in nums if not any(abs(n - w) < 1e-9 for w in want)]
    if bad:
        return False, f"the reply says {', '.join(map(str, bad))}, but this order is {plain(order)}"
    allowed = set(FILLER) | {order["side"], {"buy": "long", "sell": "short"}[order["side"]]}
    extra = [w for w in re.findall(r"[a-z]+", rest.lower()) if w not in allowed]
    if extra:
        return False, (f"the reply has words that are not part of this order ({', '.join(sorted(set(extra)))});"
                       f' a yes for this ticket is just "yes" for {plain(order)}')
    return True, f"explicit yes for {plain(order)}"


def ticket(order: dict[str, Any], idx: int, total: int) -> str:
    st, why = status_of(order)
    c, m = order.get("costs", {}), order.get("margin", {})
    lines = [
        f"── Ticket {idx}/{total}  [{st}]  {why}",
        f"   {plain(order)}  (leverage {order['leverage']:g}x, margin ${m.get('required', 0):.2f},"
        f" notional ${m.get('notional', 0):.2f})",
        f"   stop {order.get('stop_loss')} (mark-price trigger, exchange-side)   "
        f"target {order.get('take_profit')}   est. liquidation {order.get('liquidation_price_est')}",
        f"   loss at stop incl. fees+GST ${c.get('loss_at_stop_with_fees')}   "
        f"net gain at target ${c.get('net_gain_at_target')}",
        f"   why: {order.get('why', '')}",
        f"   Delta POST /v2/orders: {order.get('delta_body')}",
    ]
    return "\n".join(lines)


def approve(orders: list[dict[str, Any]], index: int, reply: str) -> dict[str, Any]:
    o = orders[index - 1]
    st, why = status_of(o)
    if st != "READY":
        raise BrokerError(f"ticket {index} is {st}: {why}")
    ok, msg = parse_yes(reply, o, tuple(x["symbol"] for x in orders))
    if not ok:
        raise BrokerError(msg)
    return record("approved", o, reply=reply, note=msg)


def decline(orders: list[dict[str, Any]], index: int, reply: str = "no") -> dict[str, Any]:
    return record("declined", orders[index - 1], reply=reply)


def place(orders: list[dict[str, Any]], index: int, executor: Any, mode: str) -> dict[str, Any]:
    """Send ONE approved order to the executor (paper or live)."""
    from infrastructure.models import TradePlan
    o = orders[index - 1]
    fp = fingerprint(o)
    d = decision(fp)
    if d is None or d["event"] != "approved":
        raise BrokerError(f"ticket {index} has no explicit yes today (status: {status_of(o)[0]})")
    rv = latest_verdict(fp)
    if rv is None or rv["verdict"] != "PASS":
        raise BrokerError("the risk verdict for this exact order is missing or not PASS")
    if o.get("reduce_only"):
        res = executor.close_position(o["symbol"], "desk_flatten")
        rec = record("placed", o, mode=mode, result=getattr(res, "state", None))
        return rec
    plan = TradePlan(o["symbol"], o["side"], int(o["size"]),
                     float(o.get("limit_price") or o["plan"]["entry"]), float(o["stop_loss"]),
                     float(o["take_profit"]) if o.get("take_profit") else None,
                     float(o["leverage"]), "desk", order_type=o.get("order_type", "market_order"),
                     limit_price=float(o["limit_price"]) if o.get("limit_price") else None,
                     plan_id="plan_" + o["client_order_id"][3:])
    try:
        res = executor.execute_plan(plan)
    except Exception as exc:
        record("failed", o, mode=mode, error=str(exc))
        raise
    executor.save()
    return record("placed", o, mode=mode, state=res.state, filled=res.filled,
                  avg_price=res.avg_fill_price, exchange_id=res.exchange_id,
                  reject_reason=res.reject_reason)


def flatten_ticket(symbol: str, pos: Any, leverage: float) -> dict[str, Any]:
    side = "sell" if pos.size > 0 else "buy"
    return {"symbol": symbol, "side": side, "size": abs(pos.size), "order_type": "market_order",
            "limit_price": None, "leverage": leverage, "stop_loss": None, "take_profit": None,
            "reduce_only": True, "client_order_id": new_id("cls")[:32],
            "plan": {"entry": pos.entry_price}, "why": "close the whole position (reduce-only)"}


def todays_log() -> list[dict[str, Any]]:
    d = utc_today()
    return [r for r in load_state()["log"] if r.get("day") == d]
