"""
Crypto chart scanner (crypto-native rebuild of trader-scanner).

Stage 1 — one public call (GET /v2/tickers?contract_types=perpetual_futures):
  liquidity (24 h USD turnover), spread, open interest, funding, 24 h change.
  Illiquid / wide / non-live contracts are skipped with a reason.
Stage 2 — closed candles for the most liquid survivors, setups:
  range_breakout   last closed bar closes beyond the prior N-bar high/low on volume
  utc_range        breaks the first-hours range of the current UTC day
                   (the crypto analogue of an opening-range breakout: Asia session)
  momentum         ROC with EMA(20) > EMA(50) alignment and price beyond rolling VWAP
  squeeze          Bollinger bands inside Keltner channels (compression → breakout watch)
  volume_surge     last bar volume >= K × its 20-bar average
  funding_extreme  |funding| >= threshold: crowded side → contrarian watch only
  liquidity_sweep  wick through a range extreme that closes back inside
Writes trader/watchlist.json. It never trades.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from desk.common import Source
from infrastructure.workspace import now_iso, write_json
from layers.technical import indicators as ta
from strategies.liquidity_sweep import LiquiditySweep


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def stage1(rows: list[dict[str, Any]], cfg: dict[str, Any],
           symbols: list[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    keep, skipped = [], {}
    blocked = {s.upper() for s in cfg.get("blocked", [])}
    allowed = {s.upper() for s in cfg.get("allowed_symbols", [])}
    for r in rows:
        sym = r.get("symbol", "")
        if r.get("contract_type") not in (None, "perpetual_futures"):
            continue
        if symbols and sym not in symbols:
            continue
        if sym in blocked:
            skipped[sym] = "blocked in config"
            continue
        if allowed and sym not in allowed:
            skipped[sym] = "not in allowed_symbols"
            continue
        q = r.get("quotes") or {}
        bid, ask = _f(q.get("best_bid")), _f(q.get("best_ask"))
        last = _f(r.get("close")) or _f(r.get("mark_price"))
        turnover = _f(r.get("turnover_usd")) or 0.0
        spread = (ask - bid) / ((ask + bid) / 2) if bid and ask and bid > 0 else None
        if not last:
            skipped[sym] = "no price"
            continue
        if turnover < cfg["min_turnover_usd_24h"]:
            skipped[sym] = f"24h turnover ${turnover:,.0f} < ${cfg['min_turnover_usd_24h']:,.0f}"
            continue
        if spread is not None and spread > cfg["max_spread_pct"]:
            skipped[sym] = f"spread {spread:.3%} > {cfg['max_spread_pct']:.3%}"
            continue
        fr = _f(r.get("funding_rate"))
        keep.append({"symbol": sym, "price": last, "mark": _f(r.get("mark_price")),
                     "turnover_usd_24h": turnover, "spread_pct": spread,
                     "funding_rate": fr / 100 if fr is not None else None,
                     "oi_contracts": _f(r.get("oi_contracts") or r.get("oi")),
                     "change_24h_pct": _f(r.get("mark_change_24h"))})
    keep.sort(key=lambda x: -x["turnover_usd_24h"])
    return keep, skipped


def find_setups(bars: pd.DataFrame, opts: dict[str, Any]) -> list[dict[str, Any]]:
    """Setups on CLOSED bars. Each hit: setup, side, level, note."""
    hits: list[dict[str, Any]] = []
    n = opts.get("lookback", 48)
    if len(bars) < max(60, n + 2):
        return hits
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    last = float(c.iloc[-1])
    vol_avg = float(v.iloc[-21:-1].mean())
    vol_x = float(v.iloc[-1] / vol_avg) if vol_avg > 0 else 0.0
    atr = float(ta.atr(bars, 14).iloc[-1])
    atr_pct = atr / last if last else 0.0
    prior_hi, prior_lo = float(h.iloc[-n - 1:-1].max()), float(l.iloc[-n - 1:-1].min())
    base = {"atr": atr, "atr_pct": round(atr_pct, 5), "volume_x": round(vol_x, 2)}

    if last > prior_hi and vol_x >= opts.get("breakout_volume_x", 1.5):
        hits.append({**base, "setup": "range_breakout", "side": "buy", "level": prior_hi,
                     "range": {"high": prior_hi, "low": prior_lo, "bars": n},
                     "note": f"closed above the {n}-bar high on {vol_x:.1f}x volume"})
    if last < prior_lo and vol_x >= opts.get("breakout_volume_x", 1.5):
        hits.append({**base, "setup": "range_breakout", "side": "sell", "level": prior_lo,
                     "range": {"high": prior_hi, "low": prior_lo, "bars": n},
                     "note": f"closed below the {n}-bar low on {vol_x:.1f}x volume"})

    # UTC-day first-hours range (Asia session) breakout
    hours = opts.get("utc_range_hours", 4)
    day = bars.index[-1].normalize()
    today = bars[bars.index >= day]
    rng_bars = today[today.index < day + pd.Timedelta(hours=hours)]
    after = today[today.index >= day + pd.Timedelta(hours=hours)]
    if len(rng_bars) >= 2 and len(after) >= 1:
        rh, rl = float(rng_bars["high"].max()), float(rng_bars["low"].min())
        prev_close = float(after["close"].iloc[-2]) if len(after) >= 2 else float(rng_bars["close"].iloc[-1])
        if last > rh >= prev_close:
            hits.append({**base, "setup": "utc_range", "side": "buy", "level": rh,
                         "range": {"high": rh, "low": rl, "hours": hours},
                         "note": f"first close above the 00:00-{hours:02d}:00 UTC range"})
        elif last < rl <= prev_close:
            hits.append({**base, "setup": "utc_range", "side": "sell", "level": rl,
                         "range": {"high": rh, "low": rl, "hours": hours},
                         "note": f"first close below the 00:00-{hours:02d}:00 UTC range"})

    ema20, ema50 = ta.ema(c, 20), ta.ema(c, 50)
    roc = float(ta.roc(c, 12).iloc[-1])
    vw = float(ta.rolling_vwap(bars, min(288, len(bars))).iloc[-1])
    if ema20.iloc[-1] > ema50.iloc[-1] and last > vw and roc >= opts.get("momentum_roc_pct", 1.0):
        hits.append({**base, "setup": "momentum", "side": "buy", "level": vw,
                     "note": f"ROC(12) {roc:+.2f}% with EMA20>EMA50, above rolling VWAP"})
    if ema20.iloc[-1] < ema50.iloc[-1] and last < vw and roc <= -opts.get("momentum_roc_pct", 1.0):
        hits.append({**base, "setup": "momentum", "side": "sell", "level": vw,
                     "note": f"ROC(12) {roc:+.2f}% with EMA20<EMA50, below rolling VWAP"})

    bu, _, bl = ta.bollinger_bands(c, 20, 2.0)
    ku, _, kl = ta.keltner(bars, 20, 1.5)
    sq = ((bu < ku) & (bl > kl)).iloc[-6:]
    if bool(sq.all()):
        hits.append({**base, "setup": "squeeze", "side": None, "level": None,
                     "range": {"high": float(h.iloc[-20:].max()), "low": float(l.iloc[-20:].min())},
                     "note": "Bollinger inside Keltner for 6+ bars: volatility compression"})

    if vol_x >= opts.get("surge_volume_x", 3.0):
        side = "buy" if c.iloc[-1] > bars["open"].iloc[-1] else "sell"
        hits.append({**base, "setup": "volume_surge", "side": side, "level": last,
                     "note": f"volume {vol_x:.1f}x its 20-bar average"})

    sig = LiquiditySweep().on_bar("", bars)
    if sig.is_actionable:
        hits.append({**base, "setup": "liquidity_sweep", "side": sig.side,
                     "level": sig.metadata.get("range_low" if sig.side == "buy" else "range_high"),
                     "note": "wick through the range extreme closed back inside"})
    return hits


def scan(source: Source, cfg: dict[str, Any], resolution: str = "5m", top: int = 15,
         symbols: list[str] | None = None, opts: dict[str, Any] | None = None,
         write: bool = True) -> dict[str, Any]:
    opts = opts or {}
    s1, skipped = stage1(source.tickers(), cfg, symbols)
    hits: list[dict[str, Any]] = []
    funding_thr = opts.get("funding_extreme", 0.0005)
    for row in s1:
        fr = row.get("funding_rate")
        if fr is not None and abs(fr) >= funding_thr:
            hits.append({"symbol": row["symbol"], "setup": "funding_extreme",
                         "side": "sell" if fr > 0 else "buy", "price": row["price"],
                         "funding_rate": fr, "level": None,
                         "note": f"funding {fr:+.4%}/interval: crowded "
                                 f"{'longs' if fr > 0 else 'shorts'} (contrarian watch only)"})
    for row in s1[:top]:
        try:
            bars = source.candles(row["symbol"], resolution, opts.get("bars", 600))
        except Exception as exc:
            skipped[row["symbol"]] = f"no candles: {exc}"
            continue
        for hit in find_setups(bars, opts):
            hits.append({"symbol": row["symbol"], "price": float(bars["close"].iloc[-1]),
                         "bar_close_utc": (bars.index[-1] + pd.Timedelta(seconds=_res_s(resolution)))
                         .isoformat(), "turnover_usd_24h": row["turnover_usd_24h"],
                         "funding_rate": row.get("funding_rate"), "spread_pct": row.get("spread_pct"),
                         **hit})
    btc = next((r for r in s1 if r["symbol"] == "BTCUSD"), None)
    frs = [r["funding_rate"] for r in s1 if r.get("funding_rate") is not None]
    doc = {
        "created_by": "desk-scanner", "created_at": now_iso(), "resolution": resolution,
        "universe": len(s1), "skipped": skipped,
        "context": {
            "btc_change_24h_pct": btc.get("change_24h_pct") if btc else None,
            "median_funding_rate": float(np.median(frs)) if frs else None,
        },
        "hits": hits,
    }
    if write:
        write_json("watchlist.json", doc)
    return doc


def _res_s(res: str) -> int:
    from infrastructure.data.market_data import RES_SECONDS
    return RES_SECONDS[res]
