"""
Price & risk alerts (crypto rebuild of trader-alerts). Rules live in
trader/alerts.json; ``check`` rings what hit (once, unless "rearm"), ``watch``
loops. Quotes come from the official public ticker (mark, last, funding, OI).

Rule types
  above / below       last (or "on": "mark") at/above or at/below "price"
  stop / target       side-aware: a long's stop rings at/below, a short's at/above
  pct_move            24 h change (or from "ref") ≥ "pct" (optional "direction")
  funding             |funding rate| ≥ "rate" (fraction per interval), optional "sign"
  funding_soon        "minutes" before the next funding time while you hold the symbol
  liq_distance        mark within "pct" % of the (estimated or exchange) liquidation
                      price of an open paper/live position
  mark_divergence     |last − mark| / mark ≥ "pct" %
  spread              bid/ask spread ≥ "pct" %
  oi_change           |open-interest change over 6 h (USD)| ≥ "usd"
  time                the UTC clock passed "at" (HH:MM); once per UTC day
Notifications: printed, appended to trader/alerts.log.jsonl and — on macOS — a
system notification (best effort).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from desk.market import next_funding
from infrastructure.workspace import append_jsonl, now_iso, read_json, write_json

TYPES = ("above", "below", "stop", "target", "pct_move", "funding", "funding_soon",
         "liq_distance", "mark_divergence", "spread", "oi_change", "time")


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_rules() -> list[dict[str, Any]]:
    return (read_json("alerts.json", {"rules": []}) or {"rules": []})["rules"]


def save_rules(rules: list[dict[str, Any]]) -> None:
    write_json("alerts.json", {"rules": rules})


def add_rule(rule: dict[str, Any]) -> dict[str, Any]:
    if rule.get("type") not in TYPES:
        raise ValueError(f"unknown alert type {rule.get('type')!r}; one of {TYPES}")
    rules = load_rules()
    rule.setdefault("id", f"{(rule.get('symbol') or 'all').lower()}-{rule['type']}-{len(rules) + 1}")
    rule.setdefault("created_at", now_iso())
    rules = [r for r in rules if r["id"] != rule["id"]] + [rule]
    save_rules(rules)
    return rule


def rules_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    sym, side = plan["symbol"], plan["side"]
    out = [{"type": "stop", "symbol": sym, "side": side, "price": plan["stop"], "on": "mark",
            "id": f"{sym.lower()}-plan-stop"},
           {"type": "funding_soon", "symbol": sym, "minutes": 15, "id": f"{sym.lower()}-plan-funding"},
           {"type": "liq_distance", "symbol": sym, "pct": 3.0, "id": f"{sym.lower()}-plan-liq"}]
    if plan.get("target"):
        out.append({"type": "target", "symbol": sym, "side": side, "price": plan["target"],
                    "id": f"{sym.lower()}-plan-target"})
    return out


def evaluate(rule: dict[str, Any], quote: dict[str, Any] | None, now: datetime,
             positions: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Return the alert message if the rule fires, else None."""
    t = rule["type"]
    if t == "time":
        hh, mm = map(int, rule["at"].split(":"))
        return f"UTC {rule['at']} reached{': ' + rule['label'] if rule.get('label') else ''}" \
            if (now.hour, now.minute) >= (hh, mm) else None
    if quote is None:
        return None
    last = _f(quote.get("close")) or _f(quote.get("mark_price"))
    mark = _f(quote.get("mark_price")) or last
    px = mark if rule.get("on") == "mark" else last
    sym = rule.get("symbol")
    if t in ("above", "below", "stop", "target"):
        lvl = float(rule["price"])
        side = rule.get("side", "buy")
        up = t == "above" or (t == "target" and side == "buy") or (t == "stop" and side == "sell")
        if (up and px >= lvl) or (not up and px <= lvl):
            return f"{sym} {'mark' if rule.get('on') == 'mark' else 'last'} {px} " \
                   f"{'≥' if up else '≤'} {lvl} ({t})"
        return None
    if t == "pct_move":
        ref = _f(rule.get("ref"))
        chg = ((last / ref - 1) * 100) if ref else _f(quote.get("mark_change_24h"))
        if chg is None:
            return None
        d = rule.get("direction")
        if abs(chg) >= float(rule["pct"]) and (d is None or (d == "up") == (chg > 0)):
            return f"{sym} moved {chg:+.2f}% ({'from ' + str(ref) if ref else '24h'})"
        return None
    if t == "funding":
        fr = _f(quote.get("funding_rate"))
        if fr is None:
            return None
        fr /= 100
        if abs(fr) >= float(rule["rate"]) and (rule.get("sign") in (None, "+" if fr > 0 else "-")):
            return f"{sym} funding {fr:+.4%} per interval ({'longs' if fr > 0 else 'shorts'} pay)"
        return None
    if t == "funding_soon":
        pos = (positions or {}).get(sym)
        if not pos:
            return None
        nf = next_funding(now, rule.get("hours_utc", [0, 8, 16]), _f(quote.get("next_funding_ts")))
        mins = (nf - now).total_seconds() / 60
        if 0 <= mins <= float(rule.get("minutes", 15)):
            fr = _f(quote.get("funding_rate"))
            return f"{sym} funding in {mins:.0f} min (rate {fr}%) on your {pos['size']:+d} contracts"
        return None
    if t == "liq_distance":
        pos = (positions or {}).get(sym)
        if not pos or not pos.get("liq"):
            return None
        dist = abs(mark - pos["liq"]) / mark * 100
        if dist <= float(rule["pct"]):
            return f"{sym} mark {mark} is {dist:.2f}% from liquidation {pos['liq']:.4f}"
        return None
    if t == "mark_divergence":
        if last and mark:
            div = abs(last - mark) / mark * 100
            if div >= float(rule["pct"]):
                return f"{sym} last {last} vs mark {mark}: {div:.2f}% apart"
        return None
    if t == "spread":
        q = quote.get("quotes") or {}
        b, a = _f(q.get("best_bid")), _f(q.get("best_ask"))
        if b and a:
            sp = (a - b) / ((a + b) / 2) * 100
            if sp >= float(rule["pct"]):
                return f"{sym} spread {sp:.3f}%"
        return None
    if t == "oi_change":
        ch = _f(quote.get("oi_change_usd_6h"))
        if ch is not None and abs(ch) >= float(rule["usd"]):
            return f"{sym} open interest changed ${ch:,.0f} in 6h"
        return None
    return None


def notify(msg: str) -> None:
    append_jsonl("alerts.log.jsonl", {"at": now_iso(), "msg": msg})
    if shutil.which("osascript"):
        try:
            subprocess.run(["osascript", "-e", f'display notification "{msg[:180]}" with title '
                            '"Delta trader alert"'], timeout=5, check=False)
        except Exception:
            pass


def check(quotes: dict[str, dict[str, Any]], positions: dict[str, dict[str, Any]] | None = None,
          now: datetime | None = None, do_notify: bool = True) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    rules = load_rules()
    rang = []
    day = now.date().isoformat()
    for r in rules:
        if r.get("fired_at") and not r.get("rearm"):
            if r["type"] in ("time", "pct_move") and not str(r["fired_at"]).startswith(day):
                pass       # daily rules re-arm each UTC day
            else:
                continue
        msg = evaluate(r, quotes.get(r.get("symbol") or ""), now, positions)
        if msg:
            r["fired_at"] = now.isoformat(timespec="seconds")
            rang.append({"id": r["id"], "msg": msg})
            if do_notify:
                notify(msg)
    save_rules(rules)
    return rang


def reset(rule_id: str) -> bool:
    rules = load_rules()
    hit = False
    for r in rules:
        if r["id"] == rule_id or rule_id == "all":
            r.pop("fired_at", None)
            hit = True
    save_rules(rules)
    return hit
