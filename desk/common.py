"""
Shared plumbing for the desk tools (the manual, skill-style workflow).

* ``desk_config()``  – trader/config.json merged over DESK_DEFAULTS
* ``Source``          – market data from the Delta India REST API, or from an
                        offline folder of saved JSON (tests, replays, no network)
* ``registry()``      – contract specs (API → cache → built-in fallback)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from infrastructure.config.settings import Settings, load_settings
from infrastructure.data.market_data import RES_SECONDS, MarketDataStore, candles_to_frame
from infrastructure.exchange.instruments import InstrumentRegistry
from infrastructure.workspace import read_json

DESK_DEFAULTS: dict[str, Any] = {
    "paper": True,                    # default: paper. Live needs the full live-approval process
    "resolution": "5m",
    "max_loss_per_trade_usd": 10.0,   # loss at the stop INCLUDING fees + GST
    "risk_per_trade_pct": None,       # alternative to the absolute limit (fraction of equity)
    "max_daily_loss_usd": 30.0,
    "max_trades_per_day": 10,
    "max_orders_per_day": 30,
    "default_leverage": 5,
    "max_leverage": 10,
    "max_open_positions": 3,
    "max_symbol_exposure_x": 2.0,
    "max_total_exposure_x": 3.0,
    "min_reward_risk": 1.5,
    "require_stop": True,
    "allow_short": True,
    "allow_options": False,           # option trading is analysis-only unless you enable it
    "allowed_symbols": [],            # empty = any live perpetual
    "blocked": [],
    "min_turnover_usd_24h": 5_000_000,
    "max_spread_pct": 0.0015,
    "max_abs_funding_rate": 0.001,
    "max_hold_hours": 8,
    "slippage_bps": 3,
    "stale_quote_seconds": 30,
    "funding_hours_utc": [0, 8, 16],  # used only when the exchange gives no next-funding time
    "paper_starting_equity_usd": 1000.0,
}


def desk_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(DESK_DEFAULTS)
    cfg.update(read_json("config.json", {}) or {})
    cfg.update(overrides or {})
    return cfg


class Source:
    """Market-data access for desk tools: REST (live) or an offline folder.

    Offline folder layout (all optional):
      tickers.json                       list of /v2/tickers rows
      products.json                      list of /v2/products rows
      candles-<SYMBOL>-<RES>.json        list of /v2/history/candles rows
      orderbook-<SYMBOL>.json            /v2/l2orderbook result
      options-<ASSET>.json               list of option /v2/tickers rows
    """

    def __init__(self, settings: Settings | None = None, offline: str | Path | None = None,
                 rest: Any = None, clock: Any = time.time) -> None:
        self.s = settings or load_settings()
        self.offline = Path(offline) if offline else None
        self._rest = rest
        self.clock = clock

    @property
    def rest(self) -> Any:
        if self._rest is None and self.offline is None:
            from infrastructure.exchange.rest import DeltaRestClient
            self._rest = DeltaRestClient(self.s.rest_url, user_agent=self.s.user_agent)
        return self._rest

    def _file(self, name: str) -> Any:
        assert self.offline is not None
        p = self.offline / name
        if not p.exists():
            raise FileNotFoundError(f"offline data missing: {p}")
        return json.loads(p.read_text())

    # ── public data ─────────────────────────────────────────────────────────

    def tickers(self, contract_types: str = "perpetual_futures") -> list[dict[str, Any]]:
        if self.offline:
            return self._file("tickers.json")
        return self.rest.get_tickers(contract_types) or []

    def ticker(self, symbol: str) -> dict[str, Any]:
        if self.offline:
            for row in self._file("tickers.json"):
                if row.get("symbol") == symbol:
                    return row
            raise KeyError(f"{symbol} not in offline tickers.json")
        return self.rest.get_ticker(symbol)

    def orderbook(self, symbol: str) -> dict[str, Any] | None:
        if self.offline:
            try:
                return self._file(f"orderbook-{symbol}.json")
            except FileNotFoundError:
                return None
        try:
            return self.rest.get_orderbook(symbol)
        except Exception:
            return None

    def candles(self, symbol: str, resolution: str, bars: int) -> pd.DataFrame:
        """Last ``bars`` CLOSED candles (forming bar dropped)."""
        if self.offline:
            for ext in ("json", "csv"):
                p = self.offline / f"candles-{symbol}-{resolution}.{ext}"
                if p.exists():
                    if ext == "json":
                        df = candles_to_frame(json.loads(p.read_text()))
                    else:
                        df = pd.read_csv(p, index_col="ts", parse_dates=["ts"])
                        if df.index.tz is None:
                            df.index = df.index.tz_localize("UTC")
                    break
            else:
                raise FileNotFoundError(f"offline candles missing for {symbol} {resolution}")
        else:
            step = RES_SECONDS[resolution]
            end = int(self.clock())
            frames = []
            start = end - bars * step - step
            cur = start
            while cur < end:
                chunk_end = min(end, cur + 2000 * step)
                frames.append(candles_to_frame(self.rest.get_candles(symbol, resolution, cur,
                                                                     chunk_end)))
                cur = chunk_end
            df = pd.concat(frames) if frames else candles_to_frame([])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        now = self.clock()
        step = RES_SECONDS[resolution]
        df = df[[ts.timestamp() + step <= now for ts in df.index]] if len(df) else df
        return df.tail(bars)

    def option_tickers(self, asset: str, expiry: str | None = None) -> list[dict[str, Any]]:
        if self.offline:
            return self._file(f"options-{asset}.json")
        params: dict[str, Any] = {"contract_types": "call_options,put_options",
                                  "underlying_asset_symbols": asset}
        if expiry:
            params["expiry_date"] = expiry
        return self.rest.request("GET", "/v2/tickers", params) or []

    def products(self) -> list[dict[str, Any]]:
        if self.offline:
            try:
                return self._file("products.json")
            except FileNotFoundError:
                return []
        return self.rest.get_products()

    # ── helpers ─────────────────────────────────────────────────────────────

    def registry(self) -> InstrumentRegistry:
        if self.offline:
            rows = self.products()
            return InstrumentRegistry.from_rows(rows) if rows else InstrumentRegistry.fallback()
        return InstrumentRegistry.load(self.rest, self.s.data_dir / "instruments.json")

    def snapshot_store(self, symbols: list[str], resolution: str = "5m") -> MarketDataStore:
        """A MarketDataStore filled from REST/offline snapshots (paper desk fills)."""
        store = MarketDataStore(resolution, clock=self.clock)
        for sym in symbols:
            try:
                store.ingest_rest_ticker(self.ticker(sym))
            except Exception:
                continue
            ob = self.orderbook(sym)
            if ob:
                store.handle({"type": "l2_orderbook", "symbol": sym, **ob})
        return store
