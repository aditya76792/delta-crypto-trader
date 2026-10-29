"""
Liquidity sweep reversal (crypto version of the equity liquidity_sweep strategy).

Crypto order flow repeatedly runs stops above prior swing highs / below swing lows
(often amplified by liquidation cascades). Setup:
  * the current bar wicks beyond the highest high / lowest low of ``lookback`` bars
  * but CLOSES back inside that range (failed breakout = sweep)
  * wick is at least ``min_wick_atr`` ATRs long
Stop   : beyond the sweep wick + buffer      Target: rr × risk, or opposite range edge
"""

from __future__ import annotations

import pandas as pd

from layers.technical import indicators as ta
from strategies.base import BaseStrategy, TradeSignal


class LiquiditySweep(BaseStrategy):
    strategy_id = "liquidity_sweep"
    holding = "scalp"
    required_warmup_bars = 60
    max_hold_bars = 24
    default_params = {
        "lookback": 36, "atr_period": 14, "min_wick_atr": 0.6, "sl_buffer_atr": 0.2,
        "rr": 2.0, "use_range_target": True,
    }

    def on_bar(self, symbol: str, bars: pd.DataFrame) -> TradeSignal:
        if len(bars) < self.required_warmup_bars:
            return TradeSignal.flat()
        prior = bars.iloc[-self.p("lookback") - 1:-1]
        hi, lo = float(prior["high"].max()), float(prior["low"].min())
        bar = bars.iloc[-1]
        a = float(ta.atr(bars, self.p("atr_period")).iloc[-1])
        if a <= 0:
            return TradeSignal.flat()
        o, h, low_, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        meta = {"atr": a, "range_high": hi, "range_low": lo}
        # Bullish sweep of lows
        lower_wick = min(o, c) - low_
        if low_ < lo and c > lo and lower_wick >= self.p("min_wick_atr") * a:
            sl = low_ - self.p("sl_buffer_atr") * a
            risk = c - sl
            tp = c + self.p("rr") * risk
            if self.p("use_range_target"):
                tp = min(tp, hi) if hi > c + risk else tp
            return TradeSignal("BUY", min(1.0, lower_wick / (2 * a)), sl, tp, meta)
        upper_wick = h - max(o, c)
        if h > hi and c < hi and upper_wick >= self.p("min_wick_atr") * a:
            sl = h + self.p("sl_buffer_atr") * a
            risk = sl - c
            tp = c - self.p("rr") * risk
            if self.p("use_range_target"):
                tp = max(tp, lo) if lo < c - risk else tp
            return TradeSignal("SELL", min(1.0, upper_wick / (2 * a)), sl, tp, meta)
        return TradeSignal.flat()
