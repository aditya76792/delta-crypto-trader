"""
Account view for the desk: the paper account (default) or the live Delta wallet.

Paper: an engines.paper_executor.PaperExecutor persisted under trader/paper/
(state.json + fills.jsonl ledger, one record per fill/funding payment), working
against REST/offline snapshots — the same matching engine the real-time loop uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from desk.common import Source
from engines.paper_executor.executor import PaperExecutor
from infrastructure.workspace import path, read_json, read_jsonl


def paper_executor(source: Source, cfg: dict[str, Any], symbols: list[str]) -> PaperExecutor:
    state = path("paper/state.json")
    held = list((read_json(state, {}) or {}).get("positions", {}).keys())
    want = sorted(set(symbols) | set(held))
    store = source.snapshot_store(want)
    reg = source.registry()
    return PaperExecutor(reg, store, float(cfg["paper_starting_equity_usd"]),
                         float(cfg["slippage_bps"]), state_path=state, clock=source.clock,
                         fill_log=path("paper/fills.jsonl"))


def today_activity(now: datetime | None = None) -> dict[str, Any]:
    """Realised P&L net of fees/funding, trades and orders for the current UTC day."""
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    pnl = fees = funding = 0.0
    entries = 0
    for r in read_jsonl(path("paper/fills.jsonl")):
        if not str(r.get("ts", "")).startswith(day):
            continue
        if "funding" in r:
            funding += float(r["funding"])
            continue
        pnl += float(r.get("realised", 0))
        fees += float(r.get("fee", 0))
        if not r.get("reduce_only"):
            entries += 1
    log = (read_json("state/broker.json", {}) or {}).get("log", [])
    placed = [x for x in log if x.get("event") == "placed" and str(x.get("at", "")).startswith(day)]
    return {"day": day, "realised_net": round(pnl - fees - funding, 6), "fees": round(fees, 6),
            "funding": round(funding, 6), "entry_fills": entries, "orders_placed": len(placed)}


def exposure(ex: Any, registry: Any, store: Any) -> tuple[dict[str, float], float]:
    per: dict[str, float] = {}
    for sym, p in ex.positions().items():
        t = store.tickers.get(sym)
        px = (t.mark or t.last) if t else p.entry_price
        per[sym] = abs(registry.get(sym).notional(abs(p.size), px))
    return per, sum(per.values())
