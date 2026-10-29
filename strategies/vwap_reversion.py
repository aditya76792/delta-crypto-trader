"""
Rolling-VWAP mean reversion (intraday/scalp).

Crypto has no session open, so the anchor is a rolling 24h VWAP (configurable).
Entry  : price stretched beyond VWAP ± band_z × rolling σ(price − VWAP) with RSI
         extreme and a rejection candle (close back inside the prior bar's range)
Filter : skip when trend is strong (|EMA-slope| in ATRs) — fading trends gets run over
Stop   : beyond the extreme of the last ``swing`` bars + atr_buffer × ATR
Target : VWAP (mean)
"""

from __future__ import annotations

import pandas as pd

from layers.technical import indicators as ta
from strategies.base import BaseStrategy, TradeSignal


class VWAPReversion(BaseStrategy):
    strategy_id = "vwap_reversion"
    holding = "intraday"
    required_warmup_bars = 300
    max_hold_bars = 24
    default_params = {
        "vwap_window": 288, "dev_window": 100, "band_z": 2.2, "rsi_period": 7,
        "rsi_low": 22, "rsi_high": 78, "swing": 5, "atr_period": 14, "atr_buffer": 1.0,
        "trend_ema": 100, "max_trend_atr": 2.5, "min_rr": 1.2,
    }

    def on_bar(self, symbol: str, bars: pd.DataFrame) -> TradeSignal:
        if len(bars) < self.required_warmup_bars:
            return TradeSignal.flat()
        c = bars["close"]
        vw = ta.rolling_vwap(bars, self.p("vwap_window"))
        dev = c - vw
        sd = dev.rolling(self.p("dev_window")).std()
        rsi = ta.rsi(c, self.p("rsi_period"))
        atr = ta.atr(bars, self.p("atr_period"))
        trend = ta.ema(c, self.p("trend_ema"))
        price, a, v, s = float(c.iloc[-1]), float(atr.iloc[-1]), float(vw.iloc[-1]), float(sd.iloc[-1])
        if not (a > 0 and s > 0) or pd.isna(v):
            return TradeSignal.flat()
        slope_atr = abs(trend.iloc[-1] - trend.iloc[-6]) / a
        if slope_atr > self.p("max_trend_atr"):
            return TradeSignal.flat()
        z = (price - v) / s
        prev = bars.iloc[-2]
        meta = {"atr": a, "vwap": v, "z": z, "rsi": float(rsi.iloc[-1])}
        lo = float(bars["low"].iloc[-self.p("swing"):].min())
        hi = float(bars["high"].iloc[-self.p("swing"):].max())

        if z <= -self.p("band_z") and rsi.iloc[-1] <= self.p("rsi_low") and price > prev["low"]:
            sl = lo - self.p("atr_buffer") * a
            if (v - price) >= self.p("min_rr") * (price - sl):
                return TradeSignal("BUY", min(1.0, abs(z) / 4), sl, v, meta)
        if z >= self.p("band_z") and rsi.iloc[-1] >= self.p("rsi_high") and price < prev["high"]:
            sl = hi + self.p("atr_buffer") * a
            if (price - v) >= self.p("min_rr") * (sl - price):
                return TradeSignal("SELL", min(1.0, abs(z) / 4), sl, v, meta)
        return TradeSignal.flat()
