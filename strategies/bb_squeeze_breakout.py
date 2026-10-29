"""
Volatility-squeeze breakout (crypto adaptation of the VT breakout/consolidation set).

Squeeze : Bollinger bands inside Keltner channels for >= min_squeeze_bars
Trigger : close breaks the squeeze range high/low with volume >= vol_mult × average
Stop    : opposite side of the squeeze range, capped at max_sl_atr × ATR
Target  : rr × stop distance; trailing exit when supertrend flips
"""

from __future__ import annotations

import pandas as pd

from layers.technical import indicators as ta
from strategies.base import BaseStrategy, TradeSignal


class BBSqueezeBreakout(BaseStrategy):
    strategy_id = "bb_squeeze_breakout"
    holding = "intraday"
    required_warmup_bars = 120
    max_hold_bars = 72
    default_params = {
        "bb_period": 20, "bb_std": 2.0, "kc_period": 20, "kc_mult": 1.5,
        "min_squeeze_bars": 6, "lookback": 20, "vol_mult": 1.5, "atr_period": 14,
        "max_sl_atr": 2.5, "rr": 2.5, "st_period": 10, "st_mult": 3.0, "allow_short": True,
    }

    def on_bar(self, symbol: str, bars: pd.DataFrame) -> TradeSignal:
        if len(bars) < self.required_warmup_bars:
            return TradeSignal.flat()
        c = bars["close"]
        bu, _, bl = ta.bollinger_bands(c, self.p("bb_period"), self.p("bb_std"))
        ku, _, kl = ta.keltner(bars, self.p("kc_period"), self.p("kc_mult"))
        squeeze = (bu < ku) & (bl > kl)
        n = self.p("min_squeeze_bars")
        # squeeze must have held on the bars BEFORE the breakout bar
        if not bool(squeeze.iloc[-n - 1:-1].all()):
            return TradeSignal.flat()
        rng = bars.iloc[-self.p("lookback") - 1:-1]
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        a = float(ta.atr(bars, self.p("atr_period")).iloc[-1])
        vol_ok = bars["volume"].iloc[-1] >= self.p("vol_mult") * bars["volume"].iloc[-21:-1].mean()
        price = float(c.iloc[-1])
        if a <= 0 or not vol_ok:
            return TradeSignal.flat()
        max_d = self.p("max_sl_atr") * a
        meta = {"atr": a, "range_high": hi, "range_low": lo}
        if price > hi:
            sl = max(lo, price - max_d)
            return TradeSignal("BUY", 0.7, sl, price + self.p("rr") * (price - sl), meta)
        if price < lo and self.p("allow_short"):
            sl = min(hi, price + max_d)
            return TradeSignal("SELL", 0.7, sl, price - self.p("rr") * (sl - price), meta)
        return TradeSignal.flat()

    def should_exit(self, symbol, bars, side, entry_price, bars_held):
        base = super().should_exit(symbol, bars, side, entry_price, bars_held)
        if base:
            return base
        _, d = ta.supertrend(bars.tail(200), self.p("st_period"), self.p("st_mult"))
        if side == "buy" and d.iloc[-1] < 0:
            return TradeSignal.exit("supertrend_flip")
        if side == "sell" and d.iloc[-1] > 0:
            return TradeSignal.exit("supertrend_flip")
        return None
