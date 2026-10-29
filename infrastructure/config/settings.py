"""
Runtime settings.

Precedence: process environment > .env file > config/config.yaml > defaults.
Secrets (DELTA_API_KEY / DELTA_API_SECRET) are read ONLY from the environment
or .env (which is git-ignored) and are never logged or written anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]

REST_URLS = {
    "prod": "https://api.india.delta.exchange",
    "testnet": "https://cdn-ind.testnet.deltaex.org",
}
# Official docs (Websocket Feed, checked 2026-10-29): private channels (orders,
# positions, user trades) on the private endpoint after key-auth; market data on the
# public endpoint with the compact message format (ticker, ob_l2, trades, candlestick_*).
WS_URLS = {
    "prod": "wss://socket.india.delta.exchange",
    "testnet": "wss://socket-ind.testnet.deltaex.org",
}
WS_PUBLIC_URLS = {
    "prod": "wss://public-socket.india.delta.exchange",
    "testnet": "wss://socket-ind-pub.testnet.deltaex.org",
}

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, does not override existing env vars."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


@dataclass
class RiskSettings:
    risk_per_trade_pct: float = 0.005        # 0.5 % of equity at risk per trade
    max_daily_loss_pct: float = 0.03         # stop trading for the UTC day at -3 %
    max_total_exposure_x: float = 3.0        # total notional <= 3x equity
    max_symbol_exposure_x: float = 2.0       # per-symbol notional <= 2x equity
    max_leverage: float = 10.0               # hard cap regardless of exchange max
    max_open_positions: int = 3
    min_liq_distance_atr: float = 3.0        # liquidation >= 3 ATR beyond stop
    min_liq_buffer_pct: float = 0.5          # stop must sit before 50 % of entry->liq distance
    min_rr: float = 1.5
    max_spread_pct: float = 0.0015           # 0.15 %
    max_slippage_pct: float = 0.003
    max_abs_funding_rate: float = 0.001      # 0.1 % per interval against us
    max_mark_last_divergence_pct: float = 0.005
    stale_data_seconds: float = 15.0
    max_consecutive_losses: int = 5
    cooldown_minutes_after_loss_streak: int = 60
    max_orders_per_minute: int = 20


@dataclass
class Settings:
    env: str = "prod"                        # prod | testnet
    mode: str = "paper"                      # backtest | paper | live
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    live_confirm: str = field(default="", repr=False)
    symbols: list[str] = field(default_factory=lambda: ["BTCUSD", "ETHUSD"])
    resolution: str = "5m"
    strategy: str = "ema_trend_scalp"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    paper_starting_equity: float = 1000.0    # USD
    paper_slippage_bps: float = 2.0
    default_leverage: float = 5.0
    use_bracket_orders: bool = True
    data_dir: Path = ROOT / "data"
    log_dir: Path = ROOT / "logs"
    log_level: str = "INFO"
    user_agent: str = "delta-crypto-trader/0.1"
    risk: RiskSettings = field(default_factory=RiskSettings)

    @property
    def rest_url(self) -> str:
        return os.getenv("DELTA_REST_URL") or REST_URLS[self.env]

    @property
    def ws_url(self) -> str:
        """Private (authenticated) WebSocket endpoint."""
        return os.getenv("DELTA_WS_URL") or WS_URLS[self.env]

    @property
    def ws_public_url(self) -> str:
        """Public market-data WebSocket endpoint (compact message format)."""
        return os.getenv("DELTA_WS_PUBLIC_URL") or WS_PUBLIC_URLS[self.env]

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def live_confirmed(self) -> bool:
        return self.live_confirm == LIVE_CONFIRM_PHRASE

    def redacted(self) -> dict[str, Any]:
        return {
            "env": self.env, "mode": self.mode, "symbols": self.symbols,
            "resolution": self.resolution, "strategy": self.strategy,
            "api_key": (self.api_key[:4] + "…") if self.api_key else "",
            "rest_url": self.rest_url, "ws_url": self.ws_url, "ws_public_url": self.ws_public_url,
        }


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def load_settings(config_path: Path | None = None, *, dotenv: bool = True) -> Settings:
    if dotenv:
        load_dotenv()
    cfg: dict[str, Any] = {}
    config_path = config_path or Path(os.getenv("TRADER_CONFIG", ROOT / "config" / "config.yaml"))
    if Path(config_path).exists():
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}

    risk_cfg = {**cfg.get("risk", {})}
    risk = RiskSettings(**{k: v for k, v in risk_cfg.items() if hasattr(RiskSettings, k)})
    risk.max_daily_loss_pct = _env_float("RISK_MAX_DAILY_LOSS_PCT", risk.max_daily_loss_pct)
    risk.risk_per_trade_pct = _env_float("RISK_PER_TRADE_PCT", risk.risk_per_trade_pct)
    risk.max_leverage = _env_float("RISK_MAX_LEVERAGE", risk.max_leverage)

    symbols = os.getenv("TRADER_SYMBOLS")
    s = Settings(
        env=os.getenv("DELTA_ENV", cfg.get("env", "prod")).lower(),
        mode=os.getenv("TRADING_MODE", cfg.get("mode", "paper")).lower(),
        api_key=os.getenv("DELTA_API_KEY", ""),
        api_secret=os.getenv("DELTA_API_SECRET", ""),
        live_confirm=os.getenv("DELTA_LIVE_CONFIRM", ""),
        symbols=[x.strip().upper() for x in symbols.split(",")] if symbols
        else cfg.get("symbols", ["BTCUSD", "ETHUSD"]),
        resolution=os.getenv("TRADER_RESOLUTION", cfg.get("resolution", "5m")),
        strategy=os.getenv("TRADER_STRATEGY", cfg.get("strategy", "ema_trend_scalp")),
        strategy_params=cfg.get("strategy_params", {}) or {},
        paper_starting_equity=_env_float("PAPER_STARTING_EQUITY_USD",
                                         float(cfg.get("paper_starting_equity", 1000.0))),
        paper_slippage_bps=float(cfg.get("paper_slippage_bps", 2.0)),
        default_leverage=_env_float("TRADER_LEVERAGE", float(cfg.get("default_leverage", 5.0))),
        use_bracket_orders=bool(cfg.get("use_bracket_orders", True)),
        data_dir=Path(os.getenv("TRADER_DATA_DIR", ROOT / "data")),
        log_dir=Path(os.getenv("TRADER_LOG_DIR", ROOT / "logs")),
        log_level=os.getenv("TRADER_LOG_LEVEL", "INFO"),
        risk=risk,
    )
    if s.env not in REST_URLS:
        raise ValueError(f"DELTA_ENV must be one of {list(REST_URLS)}, got {s.env!r}")
    if s.mode not in ("backtest", "paper", "live"):
        raise ValueError(f"TRADING_MODE must be backtest|paper|live, got {s.mode!r}")
    if s.default_leverage > s.risk.max_leverage:
        s.default_leverage = s.risk.max_leverage
    return s
