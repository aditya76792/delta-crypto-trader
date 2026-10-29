"""
Historical candles for backtesting.

* ``fetch_history`` pages GET /v2/history/candles (max 2000 bars / call) and caches
  to ``data/candles/<SYMBOL>_<RES>.csv``.
* ``synthetic_candles`` produces a reproducible GBM series with volatility
  clustering. It exists ONLY for offline tests/demos and is labelled as such;
  never use it to judge a strategy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from infrastructure.core.logger import get_logger
from infrastructure.data.market_data import RES_SECONDS, candles_to_frame

log = get_logger(__name__)
MAX_PER_CALL = 2000


def fetch_history(rest: Any, symbol: str, resolution: str, days: int,
                  cache_dir: Path | None = None, end: int | None = None) -> pd.DataFrame:
    step = RES_SECONDS[resolution]
    end = end or int(time.time())
    start = end - days * 86400
    frames = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + step * MAX_PER_CALL)
        rows = rest.get_candles(symbol, resolution, cursor, chunk_end)
        frames.append(candles_to_frame(rows))
        cursor = chunk_end
    df = pd.concat(frames) if frames else candles_to_frame([])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_dir / f"{symbol}_{resolution}.csv")
    log.info("history_fetched", symbol=symbol, resolution=resolution, bars=len(df))
    return df


def load_cached(cache_dir: Path, symbol: str, resolution: str) -> pd.DataFrame | None:
    p = cache_dir / f"{symbol}_{resolution}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col="ts", parse_dates=["ts"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def synthetic_candles(bars: int = 3000, resolution: str = "5m", start_price: float = 60000.0,
                      seed: int = 7, annual_vol: float = 0.6) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dt = RES_SECONDS[resolution] / (365 * 86400)
    base_sigma = annual_vol * np.sqrt(dt)
    # GARCH-ish vol clustering + slow regime drift so trend/mean-reversion both appear
    sig = np.empty(bars)
    s = base_sigma
    for i in range(bars):
        s = np.sqrt(0.02 * base_sigma ** 2 + 0.9 * s ** 2 + 0.08 * (rng.standard_normal() * s) ** 2)
        sig[i] = s
    drift = np.sin(np.linspace(0, 6 * np.pi, bars)) * base_sigma * 0.15
    rets = drift + sig * rng.standard_t(df=4, size=bars) / np.sqrt(2)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.standard_normal(bars)) * sig * close * 0.6
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(bars)) * sig * close * 0.6
    vol = rng.lognormal(mean=8, sigma=0.5, size=bars) * (1 + 20 * np.abs(rets))
    idx = pd.date_range("2025-01-01", periods=bars, freq=f"{RES_SECONDS[resolution]}s",
                        tz="UTC", name="ts")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=idx)
