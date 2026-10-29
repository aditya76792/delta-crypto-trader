"""
Core domain models for crypto derivatives trading on Delta Exchange India.

Replaces the equity-era models (shares, CNC/MIS products, circuit limits, sectors)
with contract-based models: every quantity is an integer number of *contracts*,
and every contract represents ``contract_value`` units of the underlying
(e.g. BTCUSD perpetual: 1 contract = 0.001 BTC).

Plain dataclasses (no pydantic) keep the hot path fast and the dependency list short.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Literal

Side = Literal["buy", "sell"]
OrderType = Literal["market_order", "limit_order"]
ContractType = Literal[
    "perpetual_futures", "futures", "call_options", "put_options",
    "move_options", "spot", "interest_rate_swaps", "spreads",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def opposite(side: Side) -> Side:
    return "sell" if side == "buy" else "buy"


def side_sign(side: Side) -> int:
    return 1 if side == "buy" else -1


# ── Contract specification ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ContractSpec:
    """
    Exchange contract specification, built from GET /v2/products.

    Delta returns margins as *percentages* ("0.5" == 0.5 %), commission
    rates as fractions ("0.0005" == 0.05 %). We store everything as fractions.
    """

    product_id: int
    symbol: str
    contract_type: str
    tick_size: float
    contract_value: float            # underlying units per contract
    underlying: str = ""
    quoting_asset: str = "USD"
    settling_asset: str = "USD"
    initial_margin: float = 0.01     # fraction, 0.01 == 1 % == 100x
    maintenance_margin: float = 0.005
    max_leverage: float = 100.0
    default_leverage: float = 10.0
    position_size_limit: int = 1_000_000  # contracts
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    min_size: int = 1                # contracts
    state: str = "live"
    is_inverse: bool = False
    funding_interval_s: int = 8 * 3600
    price_band_pct: float | None = None

    @property
    def is_perpetual(self) -> bool:
        return self.contract_type == "perpetual_futures"

    @property
    def is_option(self) -> bool:
        return self.contract_type in ("call_options", "put_options", "move_options")

    @property
    def tradable(self) -> bool:
        return self.state == "live"

    # ── Rounding helpers (exchange rejects off-tick prices / fractional lots) ──

    def round_price(self, price: float, side: Side | None = None) -> float:
        """Round to tick. Buys round down, sells round up (never cross further)."""
        tick = Decimal(str(self.tick_size))
        p = Decimal(str(price)) / tick
        if side == "buy":
            q = p.to_integral_value(rounding=ROUND_DOWN)
        elif side == "sell":
            q = p.to_integral_value(rounding=ROUND_UP)
        else:
            q = p.to_integral_value(rounding=ROUND_HALF_UP)
        return float(q * tick)

    def round_stop(self, price: float, position_side: Side) -> float:
        """Round a protective stop toward safety: a long's stop rounds up
        (slightly tighter), a short's stop rounds down."""
        return self.round_price(price, "sell" if position_side == "buy" else "buy")

    def notional(self, contracts: float, price: float) -> float:
        """Quote-currency notional value of ``contracts`` at ``price``."""
        if self.is_inverse:
            return contracts * self.contract_value
        return contracts * self.contract_value * price

    def contracts_for_notional(self, notional: float, price: float) -> int:
        if price <= 0:
            return 0
        if self.is_inverse:
            raw = notional / self.contract_value
        else:
            raw = notional / (self.contract_value * price)
        return int(math.floor(raw + 1e-9))

    def pnl(self, side: Side, contracts: float, entry: float, exit_: float) -> float:
        """Realised PnL in settling currency for a linear (or inverse) contract."""
        if self.is_inverse:
            # inverse: PnL in base currency
            gross = contracts * self.contract_value * (1 / entry - 1 / exit_)
        else:
            gross = contracts * self.contract_value * (exit_ - entry)
        return gross * side_sign(side)

    @classmethod
    def from_api(cls, p: dict[str, Any]) -> ContractSpec:
        """Build from a /v2/products result row."""

        def f(key: str, default: float) -> float:
            v = p.get(key)
            try:
                return float(v) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default

        im = f("initial_margin", 1.0) / 100.0
        mm = f("maintenance_margin", 0.5) / 100.0
        underlying = (p.get("underlying_asset") or {}).get("symbol", "") or (
            p.get("contract_unit_currency") or ""
        )
        quoting = (p.get("quoting_asset") or {}).get("symbol", "USD") if isinstance(
            p.get("quoting_asset"), dict) else (p.get("quoting_asset") or "USD")
        settling = (p.get("settling_asset") or {}).get("symbol", "USD") if isinstance(
            p.get("settling_asset"), dict) else (p.get("settling_asset") or "USD")
        band = p.get("price_band") or {}
        band_pct = None
        if isinstance(band, dict) and band.get("upper_limit_percentage"):
            band_pct = f("price_band_pct", float(band["upper_limit_percentage"])) / 100.0
        return cls(
            product_id=int(p["id"]),
            symbol=str(p["symbol"]),
            contract_type=str(p.get("contract_type", "")),
            tick_size=f("tick_size", 0.5),
            contract_value=f("contract_value", 1.0),
            underlying=underlying,
            quoting_asset=quoting,
            settling_asset=settling,
            initial_margin=im,
            maintenance_margin=mm,
            max_leverage=round(1 / im, 2) if im > 0 else 1.0,
            default_leverage=f("default_leverage", 10.0),
            position_size_limit=int(f("position_size_limit", 1_000_000)),
            maker_fee=f("maker_commission_rate", 0.0002),
            taker_fee=f("taker_commission_rate", 0.0005),
            state=str(p.get("state", "live")),
            # Delta India lists linear (USD-quoted) contracts; inverse is kept for completeness.
            is_inverse=str(p.get("contract_type", "")).startswith("inverse"),
            funding_interval_s=int(f("rate_exchange_interval", 28800) or 28800),
            price_band_pct=band_pct,
        )


# ── Market data ───────────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts: datetime          # candle OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Ticker:
    symbol: str
    last: float
    mark: float | None = None
    index: float | None = None   # spot / index price
    best_bid: float | None = None
    best_ask: float | None = None
    funding_rate: float | None = None   # fraction per funding interval
    open_interest: float | None = None  # contracts
    next_funding_ts: float | None = None   # unix seconds of the next funding realisation
    funding_interval_s: int | None = None
    turnover_usd_24h: float | None = None
    change_24h_pct: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    band_low: float | None = None
    band_high: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    oi_change_usd_6h: float | None = None
    ts: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.last

    @property
    def spread_pct(self) -> float | None:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            return (self.best_ask - self.best_bid) / self.mid
        return None


@dataclass
class BookLevel:
    price: float
    size: float   # contracts


@dataclass
class OrderBook:
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)   # best first (desc)
    asks: list[BookLevel] = field(default_factory=list)   # best first (asc)
    ts: datetime = field(default_factory=utcnow)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def imbalance(self, depth: int = 10) -> float:
        b = sum(lv.size for lv in self.bids[:depth])
        a = sum(lv.size for lv in self.asks[:depth])
        return 0.0 if b + a == 0 else (b - a) / (b + a)


# ── Trading ───────────────────────────────────────────────────────────────────

@dataclass
class TradePlan:
    """A fully-specified, risk-checkable intent to open a position."""

    symbol: str
    side: Side
    contracts: int
    entry_price: float            # reference price (mark/last) at decision time
    stop_loss: float
    take_profit: float | None
    leverage: float
    strategy_id: str
    order_type: OrderType = "market_order"
    limit_price: float | None = None
    plan_id: str = field(default_factory=lambda: new_id("plan_"))
    created_at: datetime = field(default_factory=utcnow)
    status: str = "PENDING"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_contract(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def rr_ratio(self) -> float | None:
        if self.take_profit is None or self.risk_per_contract == 0:
            return None
        return abs(self.take_profit - self.entry_price) / self.risk_per_contract

    @property
    def client_order_id(self) -> str:
        # Delta limits client_order_id length; keep it short and deterministic per plan
        return self.plan_id.replace("plan_", "dct")[:32]


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: Side
    size: int
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    post_only: bool = False
    time_in_force: str = "gtc"
    exchange_id: str | None = None
    state: str = "new"           # new | open | pending | partially_filled | closed | cancelled | rejected
    filled: int = 0
    avg_fill_price: float | None = None
    fees_paid: float = 0.0
    plan_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    reject_reason: str | None = None

    @property
    def unfilled(self) -> int:
        return max(0, self.size - self.filled)

    @property
    def is_terminal(self) -> bool:
        return self.state in ("closed", "cancelled", "rejected")


@dataclass
class Fill:
    order_client_id: str
    symbol: str
    side: Side
    size: int
    price: float
    fee: float
    is_maker: bool
    ts: datetime = field(default_factory=utcnow)
    fill_id: str = field(default_factory=lambda: new_id("fill_"))


@dataclass
class Position:
    """Net position per symbol (Delta is one-way / net mode per product)."""

    symbol: str
    size: int = 0                  # signed contracts: +long / -short
    entry_price: float = 0.0
    realised_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0      # positive == paid out
    leverage: float = 1.0
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy_id: str = ""
    plan_id: str = ""
    opened_at: datetime | None = None
    mode: str = "paper"
    exchange_liq_price: float | None = None

    @property
    def side(self) -> Side | None:
        if self.size > 0:
            return "buy"
        if self.size < 0:
            return "sell"
        return None

    @property
    def is_open(self) -> bool:
        return self.size != 0

    def unrealised_pnl(self, spec: ContractSpec, mark: float) -> float:
        if not self.size:
            return 0.0
        return spec.pnl(self.side or "buy", abs(self.size), self.entry_price, mark)

    def apply_fill(self, spec: ContractSpec, side: Side, size: int, price: float) -> float:
        """
        Update net position with a fill. Returns realised PnL generated by the fill
        (gross, before fees). Handles add, reduce, close and flip.
        """
        signed = size * side_sign(side)
        realised = 0.0
        if self.size == 0 or (self.size > 0) == (signed > 0):
            new_size = self.size + signed
            # volume-weighted average entry
            self.entry_price = (
                (abs(self.size) * self.entry_price + size * price) / abs(new_size)
            )
            self.size = new_size
            if self.opened_at is None:
                self.opened_at = utcnow()
        else:
            closing = min(abs(self.size), size)
            realised = spec.pnl(self.side or "buy", closing, self.entry_price, price)
            remaining = size - closing
            self.size += closing * side_sign(side)
            if self.size == 0:
                self.entry_price = 0.0
                self.opened_at = None
                if remaining:
                    self.size = remaining * side_sign(side)
                    self.entry_price = price
                    self.opened_at = utcnow()
        self.realised_pnl += realised
        return realised
