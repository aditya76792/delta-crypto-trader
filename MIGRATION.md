# Migration: from the AlgoTrader-India (NSE equities) code to Delta Exchange India crypto

This repo was built from the Indian-market adaptation of `daytraderskill`. The original repository was not modified.

## Kept (reusable)

| Component | Status |
|---|---|
| Layered layout (`infrastructure/`, `layers/`, `engines/`, `strategies/`, `tests/`) | kept |
| `infrastructure/core/events.py`, the synchronous `EventBus` | kept; event types changed to crypto ones (TICKER, FUNDING, STALE_DATA, WS_RECONNECTED, …) |
| `strategies/base.py`, the `BaseStrategy.on_bar` / `TradeSignal` contract with no look-ahead | kept; shorts added, plus `should_exit`, time stops, typed params |
| `layers/technical/indicators.py` (pure pandas) | kept; crypto indicators added |
| Executor split (paper vs live with the same interface) and live-mode double env gate | kept and extended to 7 gates |
| `RiskChecker` → `CheckContext` / `CheckResult` with veto list | kept; rule set replaced |
| Backtester with a monte-carlo/walk-forward mindset and a pipeline gate before live | rewritten as the validation gate (full + OOS) |
| Structured JSON logging | kept; now uses the standard library and scrubs secrets |

## Replaced (stock-market assumptions → crypto)

| Equity assumption | Crypto replacement |
|---|---|
| NSE/BSE sessions 09:15–15:30 IST, holidays calendar, no entries near open/close (RR-15/16), EOD square-off | 24/7 market, UTC-day risk accounting, time stops instead of square-off, no holiday calendar |
| Shares, integer quantity, CNC/MIS products | Contracts × `contract_value`, tick and lot rounding, `reduce_only` / `post_only`, perpetual and dated futures |
| Kite Connect (Zerodha) adapter and access tokens | Delta India signed REST + WebSocket |
| Instrument master / bhavcopy / yfinance loaders | `/v2/products` specs and `/v2/history/candles` |
| STT, stamp duty, exchange txn charges, SEBI fees, DP charges | Maker/taker on notional + 18 % GST, funding payments, slippage from L2 book walk |
| Circuit limits (RR-14) | Mark/last divergence check, exchange price band data |
| Market cap, sector exposure, fundamentals, news/earnings blackout (RR-05/09/13) | Removed. Replaced by leverage, liquidation-distance, funding, spread and stale-data rules |
| NIFTY > 200-SMA regime filter (RR-10) | Per-symbol trend filters inside the strategies |
| Average-volume ≥ 500k shares (RR-08) | Spread and book-depth checks; size capped by visible depth in paper fills |
| Daily loss on realised P&L | Equity-based (realised + unrealised + fees + funding) with automatic flatten and persistent kill switch |
| No leverage / delivery positions | Leverage caps, margin-bound sizing, liquidation price, exchange position-size limits |
| Long-only swing strategies (CNC) | Long/short scalping and intraday strategies on 1m–15m bars |
| Session VWAP | Rolling 24 h VWAP and UTC-anchored VWAP |

## Not ported (out of scope for a crypto execution system)

* fundamental scoring (`layers/fundamental`)
* Screener.in scraping
* the NSE stock screener (`layers/discovery`)
* YouTube strategy ingestion
* news/sentiment scoring for Indian equities
* the Streamlit dashboard and Telegram bot (these can be re-added on top of the event bus)
