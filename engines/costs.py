"""
Delta Exchange India cost model (replaces STT/stamp-duty/brokerage equity model).

* Trading fee  = notional × rate × (1 + GST).  Rates come from the product spec
  (``maker_commission_rate`` / ``taker_commission_rate``); Delta India adds 18 % GST
  on trading fees (official fee page: "18% GST is applicable on trading fees").
* Options: fee is charged on underlying notional but capped at a % of premium
  (cap configurable, verify current cap on delta.exchange/fees).
* Funding (perpetuals): payment = position notional × funding rate, every funding
  interval. Positive rate → longs pay shorts.
* Slippage: half-spread + impact proportional to size vs. visible depth, with a floor.
"""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.models import ContractSpec, OrderBook, Side

GST_RATE = 0.18


@dataclass
class CostModel:
    gst_rate: float = GST_RATE
    option_fee_cap_pct_of_premium: float = 0.035
    min_slippage_bps: float = 1.0

    def fee(self, spec: ContractSpec, notional: float, is_maker: bool,
            premium: float | None = None) -> float:
        rate = spec.maker_fee if is_maker else spec.taker_fee
        base = abs(notional) * rate
        if spec.is_option and premium is not None:
            base = min(base, abs(premium) * self.option_fee_cap_pct_of_premium)
        return base * (1 + self.gst_rate)

    def round_trip_fee_rate(self, spec: ContractSpec, maker_entry: bool = False) -> float:
        entry = spec.maker_fee if maker_entry else spec.taker_fee
        return (entry + spec.taker_fee) * (1 + self.gst_rate)

    @staticmethod
    def funding_payment(side: Side, notional: float, funding_rate: float) -> float:
        """Amount PAID by the position (negative = received)."""
        sign = 1 if side == "buy" else -1
        return sign * abs(notional) * funding_rate

    def market_fill_price(self, side: Side, ref_price: float, contracts: int,
                          book: OrderBook | None = None, slippage_bps: float | None = None,
                          ) -> float:
        """Walk the visible book if we have one, else apply a bps slippage."""
        if book is not None:
            levels = book.asks if side == "buy" else book.bids
            if levels:
                remaining = contracts
                cost = 0.0
                for lv in levels:
                    take = min(remaining, lv.size)
                    cost += take * lv.price
                    remaining -= take
                    if remaining <= 0:
                        break
                if remaining > 0:   # book too thin: worst level + extra impact
                    worst = levels[-1].price
                    extra = worst * 0.001 * (1 if side == "buy" else -1)
                    cost += remaining * (worst + extra)
                return cost / contracts
        bps = max(self.min_slippage_bps, slippage_bps if slippage_bps is not None else 2.0)
        adj = ref_price * bps / 10_000
        return ref_price + adj if side == "buy" else ref_price - adj
