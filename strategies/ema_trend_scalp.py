"""
EMA trend scalp (crypto adaptation of the EMA 9/20 + EMA-crossover-ATR strategies).

Regime : close vs EMA(trend_period) — only trade with the higher-timeframe trend
Trigger: fast EMA crosses slow EMA in the regime direction
Filter : RSI not exhausted; ATR% inside [min_atr_pct, max_atr_pct] (avoid dead and
         blow-off markets); volume above its average
Stop   : entry ∓ atr_sl_mult × ATR      Target: entry ± rr × stop distance
Exit   : opposite cross or time stop
"""

from __future__ import annotations

import pandas as pd

from layers.technical import indicators as ta
from strategies.base import BaseStrategy, TradeSignal


class EMATrendScalp(BaseStrategy):
    strategy_id = "ema_trend_scalp"
    holding = "scalp"
    required_warmup_bars = 210
    max_hold_bars = 36
    default_params = {
        "fast": 9, "slow": 21, "trend_period": 200, "atr_period": 14,
        "atr_sl_mult": 1.5, "rr": 2.0, "rsi_period": 14, "rsi_long_max": 70,
        "rsi_short_min": 30, "min_atr_pct": 0.0008, "max_atr_pct": 0.02,
        "volume_mult": 1.0, "allow_short": True,
    }

    def on_bar(self, symbol: str, bars: pd.DataFrame) -> TradeSignal:
        if len(bars) < self.required_warmup_bars:
            return TradeSignal.flat()
        c = bars["close"]
        fast = ta.ema(c, self.p("fast"))
        slow = ta.ema(c, self.p("slow"))
        trend = ta.ema(c, self.p("trend_period"))
        atr = ta.atr(bars, self.p("atr_period"))
        rsi = ta.rsi(c, self.p("rsi_period"))
        vol_ok = bars["volume"].iloc[-1] >= self.p("volume_mult") * \
            bars["volume"].rolling(20).mean().iloc[-1]

        price, a = float(c.iloc[-1]), float(atr.iloc[-1])
        if a <= 0:
            return TradeSignal.flat()
        atr_pct = a / price
        if not (self.p("min_atr_pct") <= atr_pct <= self.p("max_atr_pct")):
            return TradeSignal.flat()

        up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
        meta = {"atr": a, "rsi": float(rsi.iloc[-1]), "trend": float(trend.iloc[-1])}
        strength = min(1.0, abs(fast.iloc[-1] - slow.iloc[-1]) / a)
        sl_d = self.p("atr_sl_mult") * a

        if up and price > trend.iloc[-1] and rsi.iloc[-1] < self.p("rsi_long_max") and vol_ok:
            return TradeSignal("BUY", strength, price - sl_d, price + self.p("rr") * sl_d, meta)
        if down and self.p("allow_short") and price < trend.iloc[-1] \
                and rsi.iloc[-1] > self.p("rsi_short_min") and vol_ok:
            return TradeSignal("SELL", strength, price + sl_d, price - self.p("rr") * sl_d, meta)
        return TradeSignal.flat()

    def should_exit(self, symbol, bars, side, entry_price, bars_held):
        base = super().should_exit(symbol, bars, side, entry_price, bars_held)
        if base:
            return base
        c = bars["close"]
        fast, slow = ta.ema(c, self.p("fast")), ta.ema(c, self.p("slow"))
        if side == "buy" and fast.iloc[-1] < slow.iloc[-1]:
            return TradeSignal.exit("opposite_cross")
        if side == "sell" and fast.iloc[-1] > slow.iloc[-1]:
            return TradeSignal.exit("opposite_cross")
        return None
