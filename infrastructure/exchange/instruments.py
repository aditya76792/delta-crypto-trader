"""
Instrument registry: contract specs for every tradable Delta India product.

Loaded from GET /v2/products and cached to ``data/instruments.json`` so the
backtester and tests can run offline. Replaces the NSE instrument master.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from infrastructure.core.logger import get_logger
from infrastructure.models import ContractSpec

log = get_logger(__name__)

# Offline fallback specs (from the official /v2/products sample + Delta India
# contract pages). Always refreshed from the API when credentials/network allow.
FALLBACK_SPECS: dict[str, dict[str, Any]] = {
    "BTCUSD": {"id": 27, "symbol": "BTCUSD", "contract_type": "perpetual_futures",
               "tick_size": "0.5", "contract_value": "0.001", "initial_margin": "0.5",
               "maintenance_margin": "0.25", "default_leverage": "200",
               "position_size_limit": 229167, "maker_commission_rate": "0.0002",
               "taker_commission_rate": "0.0005", "state": "live",
               "underlying_asset": {"symbol": "BTC"}, "quoting_asset": {"symbol": "USD"},
               "settling_asset": {"symbol": "USD"}},
    "ETHUSD": {"id": 3136, "symbol": "ETHUSD", "contract_type": "perpetual_futures",
               "tick_size": "0.05", "contract_value": "0.01", "initial_margin": "1",
               "maintenance_margin": "0.5", "default_leverage": "100",
               "position_size_limit": 100000, "maker_commission_rate": "0.0002",
               "taker_commission_rate": "0.0005", "state": "live",
               "underlying_asset": {"symbol": "ETH"}, "quoting_asset": {"symbol": "USD"},
               "settling_asset": {"symbol": "USD"}},
}


class InstrumentRegistry:
    def __init__(self, specs: dict[str, ContractSpec] | None = None,
                 cache_path: Path | None = None) -> None:
        self._specs: dict[str, ContractSpec] = dict(specs or {})
        self._by_id: dict[int, ContractSpec] = {s.product_id: s for s in self._specs.values()}
        self.cache_path = cache_path
        self.source = "memory"

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._specs

    def get(self, symbol: str) -> ContractSpec:
        try:
            return self._specs[symbol]
        except KeyError as exc:
            raise KeyError(f"Unknown instrument {symbol!r}; run `python -m main instruments`") \
                from exc

    def by_id(self, product_id: int) -> ContractSpec | None:
        return self._by_id.get(product_id)

    def all(self) -> list[ContractSpec]:
        return list(self._specs.values())

    def _ingest(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            try:
                spec = ContractSpec.from_api(row)
            except (KeyError, ValueError) as exc:
                log.warning("product_parse_failed", symbol=row.get("symbol"), error=str(exc))
                continue
            self._specs[spec.symbol] = spec
            self._by_id[spec.product_id] = spec

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> InstrumentRegistry:
        reg = cls()
        reg._ingest(rows)
        return reg

    @classmethod
    def fallback(cls) -> InstrumentRegistry:
        reg = cls.from_rows(list(FALLBACK_SPECS.values()))
        reg.source = "fallback"
        return reg

    @classmethod
    def load(cls, rest: Any | None, cache_path: Path) -> InstrumentRegistry:
        """API first, then on-disk cache, then built-in fallback."""
        if rest is not None:
            try:
                rows = rest.get_products()
                reg = cls.from_rows(rows)
                reg.cache_path = cache_path
                reg.source = "api"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(rows))
                log.info("instruments_loaded", source="api", count=len(reg.all()))
                return reg
            except Exception as exc:
                log.warning("instruments_api_failed", error=str(exc))
        if cache_path.exists():
            reg = cls.from_rows(json.loads(cache_path.read_text()))
            reg.source = "cache"
            log.info("instruments_loaded", source="cache", count=len(reg.all()))
            return reg
        log.warning("instruments_using_fallback",
                    note="specs may be outdated; refresh from the API before live trading")
        return cls.fallback()
