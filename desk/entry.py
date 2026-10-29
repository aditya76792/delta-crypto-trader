"""
Entry timer (crypto rebuild of trader-entry): watches one setup on CLOSED bars
and says the moment to get in. A wick through a level mid-bar is not an entry;
a bar that CLOSES beyond it is. It never places an order.

Setups
  range_breakout  first closed bar beyond the prior N-bar high (short: low)
  utc_range       first close beyond the 00:00–H:00 UTC range of the current day
  level           first close beyond --level
  vwap_reclaim    a close back above the rolling VWAP after a close below (short: mirror)
  strategy:<id>   any registered strategy's signal (ema_trend_scalp, vwap_reversion, …)

Status: triggered (on the latest closed bar) | waiting | expired (triggered more
than --max-age bars ago: the move is gone, do not chase) | no-data.
Crypto guards: stale quote, wide spread, funding against the side, mark/last
divergence — each becomes a warning (hard blocks live in the risk check).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from desk.common import Source
from infrastructure.data.market_data import RES_SECONDS
from infrastructure.workspace import now_iso, write_json
from layers.technical import indicators as ta
from strategies import build_strategy

SETUPS = ("range_breakout", "utc_range", "level", "vwap_reclaim")


def _trigger_index(bars: pd.DataFrame, setup: str, side: str, opts: dict[str, Any]
                   ) -> tuple[int | None, float | None, dict[str, Any]]:
    """Index (position) of the FIRST bar that triggered within the evaluation window,
    the level, and context."""
    c, h, l = bars["close"], bars["high"], bars["low"]
    long = side == "buy"
    window = opts.get("window", 24)
    start = max(1, len(bars) - window)
    ctx: dict[str, Any] = {}
    if setup == "range_breakout":
        n = opts.get("lookback", 48)
        for i in range(max(start, n), len(bars)):
            hi, lo = float(h.iloc[i - n:i].max()), float(l.iloc[i - n:i].min())
            if (long and c.iloc[i] > hi) or (not long and c.iloc[i] < lo):
                return i, (hi if long else lo), {"range": {"high": hi, "low": lo, "bars": n}}
        hi, lo = float(h.iloc[-n:].max()), float(l.iloc[-n:].min())
        return None, (hi if long else lo), {"range": {"high": hi, "low": lo, "bars": n}}
    if setup == "utc_range":
        hours = opts.get("utc_range_hours", 4)
        day = bars.index[-1].normalize()
        rng = bars[(bars.index >= day) & (bars.index < day + pd.Timedelta(hours=hours))]
        if len(rng) < 2:
            return None, None, {"reason": "the UTC range is not complete yet"}
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        ctx = {"range": {"high": hi, "low": lo, "hours": hours}}
        first = bars.index.get_loc(rng.index[-1]) + 1
        for i in range(first, len(bars)):
            if (long and c.iloc[i] > hi) or (not long and c.iloc[i] < lo):
                return i, (hi if long else lo), ctx
        return None, (hi if long else lo), ctx
    if setup == "level":
        lvl = float(opts["level"])
        for i in range(start, len(bars)):
            if (long and c.iloc[i] > lvl >= c.iloc[i - 1]) or (not long and c.iloc[i] < lvl <= c.iloc[i - 1]):
                return i, lvl, {}
        return None, lvl, {}
    if setup == "vwap_reclaim":
        vw = ta.rolling_vwap(bars, min(opts.get("vwap_window", 288), len(bars)))
        for i in range(start, len(bars)):
            if pd.isna(vw.iloc[i - 1]):
                continue
            if long and c.iloc[i - 1] < vw.iloc[i - 1] and c.iloc[i] > vw.iloc[i]:
                return i, float(vw.iloc[i]), {"vwap": float(vw.iloc[i])}
            if not long and c.iloc[i - 1] > vw.iloc[i - 1] and c.iloc[i] < vw.iloc[i]:
                return i, float(vw.iloc[i]), {"vwap": float(vw.iloc[i])}
        return None, float(vw.iloc[-1]), {"vwap": float(vw.iloc[-1])}
    raise ValueError(f"unknown setup {setup!r}")


def evaluate(bars: pd.DataFrame, setup: str = "range_breakout", side: str = "buy",
             opts: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = opts or {}
    if bars is None or len(bars) < 30:
        return {"status": "no-data", "reason": "not enough closed bars"}
    if setup.startswith("strategy:"):
        strat = build_strategy(setup.split(":", 1)[1], opts.get("strategy_params"))
        sig = strat.on_bar("", bars)
        if sig.is_actionable and (side in (None, "auto") or sig.side == side):
            return {"status": "triggered", "side": sig.side, "bar_index": len(bars) - 1,
                    "price": float(bars["close"].iloc[-1]), "level": None,
                    "suggested_stop": sig.suggested_sl, "suggested_target": sig.suggested_target,
                    "context": sig.metadata, "reason": f"{strat.strategy_id} signalled {sig.direction}"}
        return {"status": "waiting", "side": side, "price": float(bars["close"].iloc[-1]),
                "reason": f"{strat.strategy_id}: no signal on the last closed bar"}
    i, level, ctx = _trigger_index(bars, setup, side, opts)
    price = float(bars["close"].iloc[-1])
    if i is None:
        return {"status": "waiting", "side": side, "price": price, "level": level,
                "context": ctx, "reason": ctx.get("reason") or "no closed bar beyond the level yet"}
    age = len(bars) - 1 - i
    status = "triggered" if age <= opts.get("max_age", 0) else "expired"
    return {"status": status, "side": side, "bar_index": i, "bars_ago": age,
            "trigger_bar_open_utc": bars.index[i].isoformat(),
            "price": float(bars["close"].iloc[i]) if status == "expired" else price,
            "level": level, "context": ctx,
            "reason": ("closed beyond the level on the latest bar" if status == "triggered" else
                       f"triggered {age} bars ago — the move already happened; don't chase")}


def guards(ticker: dict[str, Any] | None, side: str, cfg: dict[str, Any]) -> list[str]:
    w: list[str] = []
    if not ticker:
        return ["no live quote: cannot judge spread / funding"]
    q = ticker.get("quotes") or {}
    try:
        bid, ask = float(q.get("best_bid")), float(q.get("best_ask"))
        spread = (ask - bid) / ((ask + bid) / 2)
        if spread > cfg["max_spread_pct"]:
            w.append(f"spread {spread:.3%} is wider than {cfg['max_spread_pct']:.3%}")
    except (TypeError, ValueError):
        w.append("no bid/ask in the quote")
    try:
        fr = float(ticker.get("funding_rate")) / 100
        against = fr if side == "buy" else -fr
        if against > cfg["max_abs_funding_rate"]:
            w.append(f"funding {fr:+.4%}/interval is against a {side}")
    except (TypeError, ValueError):
        pass
    try:
        last, mark = float(ticker.get("close")), float(ticker.get("mark_price"))
        if mark and abs(last - mark) / mark > 0.005:
            w.append(f"last {last} is {abs(last - mark) / mark:.2%} away from mark {mark}")
    except (TypeError, ValueError):
        pass
    return w


def check(source: Source, cfg: dict[str, Any], symbol: str, setup: str, side: str,
          resolution: str = "5m", opts: dict[str, Any] | None = None,
          write: bool = False) -> dict[str, Any]:
    opts = opts or {}
    if side == "sell" and not cfg.get("allow_short", True):
        return {"symbol": symbol, "status": "refused", "reason": "allow_short is false"}
    bars = source.candles(symbol, resolution, opts.get("bars", 600))
    res = evaluate(bars, setup, side, opts)
    try:
        tk = source.ticker(symbol)
    except Exception:
        tk = None
    doc = {"created_by": "desk-entry", "checked_at": now_iso(), "symbol": symbol,
           "setup": setup, "resolution": resolution, "interval_s": RES_SECONDS[resolution],
           **res, "warnings": guards(tk, res.get("side") or side, cfg)}
    if len(bars):
        doc["last_bar_open_utc"] = bars.index[-1].isoformat()
    if write and doc["status"] == "triggered":
        write_json("entry.json", doc)
    return doc
