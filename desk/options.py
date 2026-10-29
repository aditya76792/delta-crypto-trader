"""
Options desk (crypto rebuild of trader-fno) — ANALYSIS ONLY.

Reads Delta India's BTC/ETH option chain from the official public tickers
endpoint (GET /v2/tickers?contract_types=call_options,put_options
&underlying_asset_symbols=BTC&expiry_date=DD-MM-YYYY), normalises it and reports
ATM IV, the ATM straddle and implied move, put/call OI ratio, max pain and the
largest OI strikes. Option trading is disabled by default (allow_options=false):
fees on options are 0.01 % of notional capped at 3.5 % of premium (Delta India fee
page) — check before enabling.
Writes trader/options/chain-<ASSET>-<EXPIRY>.json.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrastructure.workspace import now_iso, write_json


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_symbol(sym: str) -> tuple[str, str, float, str] | None:
    """C-BTC-75000-310325 → ("C", "BTC", 75000.0, "310325")."""
    parts = sym.split("-")
    if len(parts) != 4 or parts[0] not in ("C", "P"):
        return None
    try:
        return parts[0], parts[1], float(parts[2]), parts[3]
    except ValueError:
        return None


def normalise(rows: list[dict[str, Any]], expiry: str | None = None) -> dict[str, Any]:
    strikes: dict[float, dict[str, Any]] = defaultdict(dict)
    spot = None
    exp_seen: set[str] = set()
    for r in rows:
        p = parse_symbol(r.get("symbol", ""))
        if not p:
            continue
        kind, asset, strike, exp = p
        exp_seen.add(exp)
        if expiry and exp != expiry:
            continue
        q = r.get("quotes") or {}
        g = r.get("greeks") or {}
        spot = spot or _f(r.get("spot_price"))
        strikes[strike]["CE" if kind == "C" else "PE"] = {
            "symbol": r["symbol"], "mark": _f(r.get("mark_price")), "bid": _f(q.get("best_bid")),
            "ask": _f(q.get("best_ask")), "iv": _f(q.get("mark_iv") or r.get("mark_vol")),
            "oi": _f(r.get("oi_contracts") or r.get("oi")) or 0.0, "delta": _f(g.get("delta")),
            "volume": _f(r.get("volume"))}
    if expiry is None and len(exp_seen) > 1:
        raise ValueError(f"several expiries in the data {sorted(exp_seen)}: pass --expiry DDMMYY")
    return {"spot": spot, "expiry": expiry or (next(iter(exp_seen)) if exp_seen else None),
            "strikes": [{"strike": k, **v} for k, v in sorted(strikes.items())]}


def analyse(chain: dict[str, Any], contract_value: float = 0.001) -> dict[str, Any]:
    rows = chain["strikes"]
    spot = chain.get("spot")
    if not rows or not spot:
        return {"error": "empty chain or no spot price"}
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))
    ce, pe = atm.get("CE") or {}, atm.get("PE") or {}
    straddle = (ce.get("mark") or 0) + (pe.get("mark") or 0)
    oi_c = sum((r.get("CE") or {}).get("oi", 0) for r in rows)
    oi_p = sum((r.get("PE") or {}).get("oi", 0) for r in rows)
    pain = []
    for r in rows:
        k = r["strike"]
        loss = sum(max(0.0, k - x["strike"]) * (x.get("CE") or {}).get("oi", 0) +
                   max(0.0, x["strike"] - k) * (x.get("PE") or {}).get("oi", 0) for x in rows)
        pain.append((loss * contract_value, k))
    max_pain = min(pain)[1] if pain else None
    top_c = sorted(rows, key=lambda r: -(r.get("CE") or {}).get("oi", 0))[:3]
    top_p = sorted(rows, key=lambda r: -(r.get("PE") or {}).get("oi", 0))[:3]
    ivs = [x for x in (ce.get("iv"), pe.get("iv")) if x]
    return {"spot": spot, "atm_strike": atm["strike"], "atm_iv": sum(ivs) / len(ivs) if ivs else None,
            "straddle": round(straddle, 4),
            "implied_move_pct": round(straddle / spot * 100, 3) if straddle else None,
            "pcr_oi": round(oi_p / oi_c, 3) if oi_c else None, "max_pain": max_pain,
            "call_oi_walls": [r["strike"] for r in top_c], "put_oi_walls": [r["strike"] for r in top_p]}


def chain_doc(asset: str, rows: list[dict[str, Any]], expiry: str | None, write: bool = True
              ) -> dict[str, Any]:
    ch = normalise(rows, expiry)
    doc = {"created_by": "desk-options", "at": now_iso(), "asset": asset, **ch,
           "analysis": analyse(ch), "note": "analysis only — options trading is off by default"}
    if write and ch.get("expiry"):
        write_json(f"options/chain-{asset}-{ch['expiry']}.json", doc)
    return doc
