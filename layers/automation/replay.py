"""
Replay feed: turns OHLCV bars into the same WebSocket message shapes the live
feed produces (v2/ticker, l2_orderbook, candlestick_<res>), so the complete
Trader → Risk → PaperExecutor pipeline can be validated end-to-end offline.

Each bar emits a price path open → (low|high) → (high|low) → close (bullish
bars visit the low first), giving intrabar stop/target fills like a live feed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd


class ReplayClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def replay_messages(bars: pd.DataFrame, symbol: str, resolution: str, clock: ReplayClock,
                    spread_pct: float = 0.0002, book_size: float = 5000,
                    funding_rate_pct: float = 0.01) -> Iterator[dict[str, Any]]:
    step = pd.Timedelta(resolution.replace("m", "min")).total_seconds() \
        if resolution.endswith("m") else pd.Timedelta(resolution).total_seconds()
    for ts, row in bars.iterrows():
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        start_us = int(ts.timestamp() * 1e6)
        clock.t = ts.timestamp()
        # first update of a new candle closes the previous one (same as the live feed)
        yield {"type": f"candlestick_{resolution}", "symbol": symbol,
               "candle_start_time": start_us, "open": o, "high": o, "low": o, "close": o,
               "volume": 0.0, "resolution": resolution}
        for k, px in enumerate(path):
            clock.t = ts.timestamp() + step * 0.999 * k / (len(path) - 1)
            half = px * spread_pct / 2
            bid, ask = round(px - half, 1), round(px + half, 1)
            yield {"type": "v2/ticker", "symbol": symbol, "close": px, "mark_price": px,
                   "spot_price": px, "quotes": {"best_bid": bid, "best_ask": ask},
                   "funding_rate": funding_rate_pct, "timestamp": int(clock.t * 1e6)}
            yield {"type": "l2_orderbook", "symbol": symbol, "timestamp": int(clock.t * 1e6),
                   "buy": [{"limit_price": bid - i * 0.5, "size": book_size} for i in range(5)],
                   "sell": [{"limit_price": ask + i * 0.5, "size": book_size} for i in range(5)]}
        clock.t = ts.timestamp() + step - 0.001
        yield {"type": f"candlestick_{resolution}", "symbol": symbol,
               "candle_start_time": start_us, "open": o, "high": h, "low": l, "close": c,
               "volume": float(row.volume), "resolution": resolution}
