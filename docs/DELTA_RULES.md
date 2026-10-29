# Delta Exchange India: the facts this system relies on

Checked on **2026-10-29** against the official API documentation (<https://docs.delta.exchange>, India site) and the Delta India help centre. Exchanges change these, so re-check before trading live. `python -m main instruments` and `python -m main doctor` read the live values.

## Endpoints
| | Production | Testnet (demo account) |
|---|---|---|
| REST | `https://api.india.delta.exchange` (`/v2/...`) | `https://cdn-ind.testnet.deltaex.org` |
| WebSocket, private (orders/positions/fills) | `wss://socket.india.delta.exchange` | `wss://socket-ind.testnet.deltaex.org` |
| WebSocket, public (market data) | `wss://public-socket.india.delta.exchange` | `wss://socket-ind-pub.testnet.deltaex.org` |

India API keys work only on the India production host; demo keys only on testnet. `api.delta.exchange` is Delta Global.

## Authentication
- REST headers: `api-key`, `timestamp` (unix **seconds**), `signature` = hex HMAC-SHA256(secret, `METHOD + timestamp + path + ?query + body`), plus a `User-Agent` header (required).
- Signatures older than ~5 s are rejected (`SignatureExpired`). The client re-signs every retry and can measure clock offset (`doctor`).
- WebSocket: `{"type":"key-auth","payload":{"api-key","signature","timestamp"}}`, where the signature is over `GET + timestamp + /live`. The old `auth` type was deprecated and stopped working after 31-Dec-2025.
- Trading-permission API keys **require an IP whitelist**. Error codes include `ip_not_whitelisted_for_api_key` and `invalid_api_key`.

## Limits
- REST: 20,000 weight units per fixed 5-minute window. Weights are 3 for products / orderbook / tickers / open orders / positions / balances / candles, 5 for place / edit / delete order, 10 for order history / fills, and 25 for a batch. A 429 returns `X-RATE-LIMIT-RESET` in ms. 500 operations/s per product. Cancels are not rate-limited.
- WebSocket: 150 connections per IP per 5 minutes. A socket with no activity within 60 s of connecting is disconnected. The heartbeat (`enable_heartbeat`) arrives every 30 s; reconnect if nothing arrives for 35 s.
- `client_order_id` is at most 32 characters. `limit_price` must be > 0 or omitted (15.04.26).
- `product_ids` / `symbols` lists are at most 10 on tickers, orders, positions/margined and fills.

## Contracts and margin (from `/v2/products`)
- `initial_margin` and `maintenance_margin` are **percentages** (BTCUSD sample: `0.5` / `0.25`, i.e. 200× max).
- Linear, USD-quoted contracts. BTCUSD: `contract_value` 0.001 BTC, tick 0.5. Orders are in whole contracts.
- The product carries `position_size_limit`, `maker_commission_rate`, `taker_commission_rate`, and the funding interval (`rate_exchange_interval`, 28800 s in the sample).
- `/v2/positions/margined` can lag ~10 s; `/v2/positions?product_id=` is real time.

## Fees (help centre, "Fees on Options and Futures Trading")
- Futures/perpetuals: **taker 0.05 %, maker 0.02 % of notional**.
- Options: 0.01 % of notional, **capped at 3.5 % of the premium**.
- **18 % GST** on trading fees (delta.exchange/fees).
- The system reads the per-product rates from `/v2/products` at runtime; the numbers above are the fallback.

## Funding
- Perpetuals pay or receive funding; a positive rate means longs pay shorts.
- WebSocket `funding_rate`: `fr` (percent per interval), `fi` (interval in seconds), `nfr` (next realisation time, µs).
- History via `/v2/history/candles?symbol=FUNDING:<SYM>`. **Assumption:** the values are percent per interval. Verify on your data.

## Orders used
`POST /v2/orders` takes `product_id`/`product_symbol`, `size`, `side`, `order_type` (`market_order`/`limit_order`), `limit_price`, `client_order_id`, `reduce_only`, `post_only`, `time_in_force` (`gtc`/`ioc`), and bracket fields `bracket_stop_loss_price`, `bracket_take_profit_price` and `bracket_stop_trigger_method` (`mark_price`).

Also used:
- `PUT /v2/orders` (edit)
- `DELETE /v2/orders` and `DELETE /v2/orders/all`
- `POST /v2/orders/bracket` (position bracket)
- `POST /v2/products/{id}/orders/leverage` with body `{"leverage": "10"}`

## Account eligibility
Delta Exchange India is operated by Excelium Technologies Pvt Ltd, an FIU-IND registered reporting entity. An account needs completed KYC. The platform lists crypto **futures and options**; use `instruments` for what is live.

## Tax
Crypto (VDA) taxation in India is specialised, and treatment of crypto derivatives is not settled. This software computes no tax. **Ask a chartered accountant.**
