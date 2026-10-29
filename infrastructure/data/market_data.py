"""
Live market-data state built from WebSocket messages (and REST bootstrap).

Keeps, per symbol: latest ticker (last/mark/index/bid/ask/funding), L2 book,
a rolling candle DataFrame, and the wall-clock time each feed last updated so
risk rules can refuse to trade on stale or missing data.

Crypto trades 24/7: there is no session open/close, candles are bucketed on
UTC epoch boundaries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from infrastructure.models import BookLevel, Candle, OrderBook, Ticker

RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> datetime:
    """Delta timestamps are micro-seconds (WS) or seconds (candles REST)."""
    if v is None:
        return datetime.now(timezone.utc)
    v = float(v)
    if v > 1e17:
        v /= 1e9
    elif v > 1e14:
        v /= 1e6
    elif v > 1e11:
        v /= 1e3
    return datetime.fromtimestamp(v, tz=timezone.utc)


def candles_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows)
    tcol = "time" if "time" in df else ("candle_start_time" if "candle_start_time" in df else "ts")
    df["ts"] = [_ts(v) for v in df[tcol]]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df.get(c, 0.0), errors="coerce")
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])


class MarketDataStore:
    def __init__(self, resolution: str = "5m", max_bars: int = 2000,
                 clock: Callable[[], float] = time.time) -> None:
        if resolution not in RES_SECONDS:
            raise ValueError(f"unsupported resolution {resolution}")
        self.resolution = resolution
        self.bar_seconds = RES_SECONDS[resolution]
        self.max_bars = max_bars
        self._clock = clock
        self.tickers: dict[str, Ticker] = {}
        self.books: dict[str, OrderBook] = {}
        self.candles: dict[str, pd.DataFrame] = {}
        self._forming: dict[str, Candle] = {}
        self._last_closed: dict[str, datetime] = {}
        self.updated_at: dict[tuple[str, str], float] = {}
        self._candle_listeners: list[Callable[[str, Candle], Any]] = []

    # ── listeners ───────────────────────────────────────────────────────────

    def on_candle_close(self, cb: Callable[[str, Candle], Any]) -> None:
        self._candle_listeners.append(cb)

    # ── freshness ───────────────────────────────────────────────────────────

    def age(self, symbol: str, feed: str = "ticker") -> float:
        t = self.updated_at.get((symbol, feed))
        return float("inf") if t is None else self._clock() - t

    def is_stale(self, symbol: str, max_age_s: float, feed: str = "ticker") -> bool:
        return self.age(symbol, feed) > max_age_s

    def _touch(self, symbol: str, feed: str) -> None:
        self.updated_at[(symbol, feed)] = self._clock()

    # ── bootstrap ───────────────────────────────────────────────────────────

    def seed_candles(self, symbol: str, df: pd.DataFrame) -> None:
        """Seed history. The last REST candle may still be forming — drop it."""
        if df.empty:
            self.candles[symbol] = df
            return
        now = self._clock()
        last_open = df.index[-1].timestamp()
        if last_open + self.bar_seconds > now:
            df = df.iloc[:-1]
        self.candles[symbol] = df.tail(self.max_bars).copy()
        if len(df):
            self._last_closed[symbol] = df.index[-1].to_pydatetime()
        self._touch(symbol, "candles")

    def close_from_rest(self, symbol: str, rows: list[dict[str, Any]]) -> list[tuple[str, Candle]]:
        """REST fallback: given recent /v2/history/candles rows, close every bar that
        has finished and has not been closed yet (the WS candle stream may be absent)."""
        df = candles_to_frame(rows)
        now = self._clock()
        out: list[tuple[str, Candle]] = []
        last = self._last_closed.get(symbol)
        for ts, r in df.iterrows():
            if ts.timestamp() + self.bar_seconds > now:
                continue                              # still forming
            if last is not None and ts.to_pydatetime() <= last:
                continue
            c = Candle(ts.to_pydatetime(), float(r.open), float(r.high), float(r.low),
                       float(r.close), float(r.volume))
            self._append(symbol, c)
            self._last_closed[symbol] = c.ts
            last = c.ts
            if self._forming.get(symbol) is not None and self._forming[symbol].ts <= c.ts:
                self._forming.pop(symbol, None)
            out.append((symbol, c))
        if out:
            self._touch(symbol, "candles")
            for s_, cc in out:
                for cb in self._candle_listeners:
                    cb(s_, cc)
        return out

    # ── WS message routing ──────────────────────────────────────────────────

    def handle(self, msg: dict[str, Any]) -> list[tuple[str, Candle]]:
        """Route a raw WS message (compact public-endpoint format or the legacy
        format). Returns the list of (symbol, closed_candle)."""
        mtype = str(msg.get("type", ""))
        if mtype == "ticker":
            self._on_ticker_compact(msg)
        elif mtype == "v2/ticker":
            self._on_ticker(msg)
        elif mtype in ("ob_l2", "l2_orderbook"):
            self._on_book(msg)
        elif mtype.startswith("candlestick_"):
            if str(msg.get("sy", msg.get("symbol", ""))).startswith("MARK:"):
                return []          # mark-price candles are informational only
            return self._on_candle(msg)
        elif mtype == "mark_price":
            sym = str(msg.get("sy") or msg.get("symbol", "")).replace("MARK:", "")
            px = _f(msg.get("p", msg.get("price")))
            t = self.tickers.get(sym)
            if t is not None and px:
                t.mark = px
                self._touch(sym, "mark")
        elif mtype == "funding_rate":
            sym = msg.get("sy") or msg.get("symbol", "")
            fr = _f(msg.get("fr", msg.get("funding_rate")))
            t = self.tickers.get(sym)
            if t is not None and fr is not None:
                t.funding_rate = fr / 100.0          # API publishes percent per interval
                nfr = msg.get("nfr") or msg.get("next_funding_realization")
                if nfr:
                    t.next_funding_ts = _ts(nfr).timestamp()
                if msg.get("fi"):
                    t.funding_interval_s = int(msg["fi"])
                self._touch(sym, "funding")
        elif mtype in ("trades", "all_trades"):
            sym = msg.get("sy") or msg.get("symbol", "")
            px = _f(msg.get("p", msg.get("price")))
            t = self.tickers.get(sym)
            if t is not None and px:
                t.last = px
                self._touch(sym, "trades")
        return []

    def _on_ticker_compact(self, m: dict[str, Any]) -> None:
        """Public endpoint ``ticker``: {"d":[{"s","m","q":[ask,askSz,bid,bidSz,impact],
        "ohlc":[o,h,l,c],"oi":[contracts,chg6h],"pb":[lo,hi],"to":[turnover,usd],
        "m24hc"}], "sp", "sy", "ts"} (docs sample, checked 2026-10-29)."""
        for d in m.get("d") or []:
            sym = d.get("s") or m.get("sy", "")
            prev = self.tickers.get(sym)
            q = d.get("q") or [None] * 5
            ohlc = d.get("ohlc") or [None] * 4
            oi = d.get("oi") or [None, None]
            pb = d.get("pb") or [None, None]
            to = d.get("to") or [None, None]
            last = _f(ohlc[3]) if len(ohlc) > 3 else None
            t = Ticker(
                symbol=sym,
                last=last or _f(d.get("m")) or (prev.last if prev else 0.0),
                mark=_f(d.get("m")),
                index=_f(m.get("sp")),
                best_ask=_f(q[0]), ask_size=_f(q[1]), best_bid=_f(q[2]), bid_size=_f(q[3]),
                funding_rate=prev.funding_rate if prev else None,
                next_funding_ts=prev.next_funding_ts if prev else None,
                funding_interval_s=prev.funding_interval_s if prev else None,
                open_interest=_f(oi[0]), oi_change_usd_6h=_f(oi[1]) if len(oi) > 1 else None,
                turnover_usd_24h=_f(to[1]) if len(to) > 1 else _f(to[0]),
                change_24h_pct=_f(d.get("m24hc")),
                high_24h=_f(ohlc[1]), low_24h=_f(ohlc[2]),
                band_low=_f(pb[0]), band_high=_f(pb[1]) if len(pb) > 1 else None,
                ts=_ts(m.get("ts")),
            )
            self.tickers[sym] = t
            self._touch(sym, "ticker")

    def _on_ticker(self, m: dict[str, Any]) -> None:
        """Legacy ``v2/ticker`` / REST /v2/tickers row (funding_rate in percent)."""
        sym = m.get("symbol", "")
        quotes = m.get("quotes") or {}
        fr = _f(m.get("funding_rate"))
        band = m.get("price_band") or {}
        t = Ticker(
            symbol=sym,
            last=_f(m.get("close")) or _f(m.get("mark_price")) or 0.0,
            mark=_f(m.get("mark_price")),
            index=_f(m.get("spot_price")),
            best_bid=_f(quotes.get("best_bid")),
            best_ask=_f(quotes.get("best_ask")),
            funding_rate=(fr / 100.0) if fr is not None else None,   # API gives percent
            open_interest=_f(m.get("oi_contracts") or m.get("oi")),
            turnover_usd_24h=_f(m.get("turnover_usd")),
            change_24h_pct=_f(m.get("mark_change_24h")),
            high_24h=_f(m.get("high")), low_24h=_f(m.get("low")),
            band_low=_f(band.get("lower_limit")) if isinstance(band, dict) else None,
            band_high=_f(band.get("upper_limit")) if isinstance(band, dict) else None,
            bid_size=_f(quotes.get("bid_size")), ask_size=_f(quotes.get("ask_size")),
            oi_change_usd_6h=_f(m.get("oi_change_usd_6h")),
            ts=_ts(m.get("timestamp")),
        )
        self.tickers[sym] = t
        self._touch(sym, "ticker")

    def ingest_rest_ticker(self, row: dict[str, Any]) -> None:
        """Seed/refresh from GET /v2/tickers/{symbol} (same shape as legacy v2/ticker)."""
        self._on_ticker(row)

    def update_ticker(self, t: Ticker) -> None:
        self.tickers[t.symbol] = t
        self._touch(t.symbol, "ticker")

    def _on_book(self, m: dict[str, Any]) -> None:
        sym = m.get("symbol", "")

        sym = m.get("sy") or sym

        def lv(rows: list[Any]) -> list[BookLevel]:
            out = []
            for r in rows or []:
                if isinstance(r, (list, tuple)):      # compact ob_l2: [price, size]
                    p, s = _f(r[0]), _f(r[1] if len(r) > 1 else None)
                else:
                    p, s = _f(r.get("limit_price") or r.get("price")), _f(r.get("size"))
                if p and s:
                    out.append(BookLevel(p, s))
            return out

        bids = sorted(lv(m.get("b") if "b" in m else m.get("buy")), key=lambda x: -x.price)
        asks = sorted(lv(m.get("a") if "a" in m else m.get("sell")), key=lambda x: x.price)
        self.books[sym] = OrderBook(sym, bids, asks, _ts(m.get("ts") or m.get("timestamp")))
        self._touch(sym, "book")

    def _on_candle(self, m: dict[str, Any]) -> list[tuple[str, Candle]]:
        sym = str(m.get("sy") or m.get("symbol", "")).replace("MARK:", "")
        if m.get("candle_start_time") is not None:
            start = _ts(m.get("candle_start_time"))
        else:   # compact format only has the update timestamp → floor to the bar
            t = _ts(m.get("ts")).timestamp()
            start = datetime.fromtimestamp(t - t % self.bar_seconds, tz=timezone.utc)
        def g(short: str, long: str) -> Any:
            return m.get(short, m.get(long))
        c = Candle(start, float(g("o", "open")), float(g("h", "high")), float(g("l", "low")),
                   float(g("c", "close")), float(g("v", "volume") or 0))
        return self.update_candle(sym, c)

    def update_candle(self, sym: str, c: Candle) -> list[tuple[str, Candle]]:
        """Feed a (possibly still forming) candle. A newer start time closes the
        previous one. Closing is idempotent per (symbol, start)."""
        closed: list[tuple[str, Candle]] = []
        prev = self._forming.get(sym)
        last_closed = self._last_closed.get(sym)
        if last_closed is not None and c.ts <= last_closed:
            return []          # late update for a bar we already closed
        if prev is not None and c.ts > prev.ts:
            self._append(sym, prev)
            self._last_closed[sym] = prev.ts
            closed.append((sym, prev))
        self._forming[sym] = c
        self._touch(sym, "candles")
        for s, cc in closed:
            for cb in self._candle_listeners:
                cb(s, cc)
        return closed

    def _append(self, sym: str, c: Candle) -> None:
        row = pd.DataFrame([{"open": c.open, "high": c.high, "low": c.low,
                             "close": c.close, "volume": c.volume}],
                           index=pd.DatetimeIndex([c.ts], name="ts"))
        df = self.candles.get(sym)
        if df is None or df.empty:
            self.candles[sym] = row
        else:
            if c.ts in df.index:
                df = df.drop(index=c.ts)
            self.candles[sym] = pd.concat([df, row]).tail(self.max_bars)

    def bars(self, symbol: str) -> pd.DataFrame:
        return self.candles.get(symbol, candles_to_frame([]))
