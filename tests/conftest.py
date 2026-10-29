from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infrastructure.config.settings import RiskSettings, Settings  # noqa: E402
from infrastructure.core.events import EventBus  # noqa: E402
from infrastructure.data.market_data import MarketDataStore  # noqa: E402
from infrastructure.exchange.instruments import InstrumentRegistry  # noqa: E402
from infrastructure.models import BookLevel, OrderBook, Ticker  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DELTA_API_KEY", "DELTA_API_SECRET", "DELTA_LIVE_CONFIRM", "TRADING_MODE",
              "DELTA_LIVE_NONINTERACTIVE", "TRADER_SYMBOLS", "DELTA_ENV"):
        monkeypatch.delenv(k, raising=False)
    EventBus.reset()
    yield
    EventBus.reset()


@pytest.fixture
def registry() -> InstrumentRegistry:
    return InstrumentRegistry.fallback()


@pytest.fixture
def btc(registry):
    return registry.get("BTCUSD")


@pytest.fixture
def risk() -> RiskSettings:
    return RiskSettings()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(symbols=["BTCUSD"], data_dir=tmp_path, log_dir=tmp_path / "logs")


class FakeClock:
    def __init__(self, t: float = 1_767_225_600.0) -> None:   # 2026-01-01 00:00 UTC
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def put_market(store: MarketDataStore, symbol: str, price: float, spread: float = 1.0,
               funding: float | None = 0.0001, depth: float = 1000.0,
               mark: float | None = None) -> None:
    bid, ask = price - spread / 2, price + spread / 2
    store.update_ticker(Ticker(symbol, last=price, mark=mark if mark is not None else price,
                               index=price, best_bid=bid, best_ask=ask, funding_rate=funding))
    store.books[symbol] = OrderBook(
        symbol, [BookLevel(bid - i * 0.5, depth) for i in range(5)],
        [BookLevel(ask + i * 0.5, depth) for i in range(5)])
