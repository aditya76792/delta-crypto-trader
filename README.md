# Delta Crypto Trader

A real-time crypto derivatives trading system for **Delta Exchange India**. It covers backtesting, paper trading on live market data, and real-money execution with strict risk controls.

It was ported from the `daytraderskill` / AlgoTrader-India equity system. The layered architecture, event bus, strategy interface, backtester and paper/live executor split were kept. Everything that assumed Indian stock markets was rewritten for 24/7 crypto perpetuals. [`MIGRATION.md`](MIGRATION.md) lists each replacement.

> ⚠️ **Real money risk.** Crypto derivatives with leverage can lose more than you expect, very quickly. This software comes with **no warranty**. Run it in paper mode first. Only trade live with money you can afford to lose, and only after a strategy passes the backtest gate on real exchange history.

---

## 1. What's inside

| Layer | Module | Purpose |
|---|---|---|
| Exchange | `infrastructure/exchange/rest.py` | Signed REST client (HMAC-SHA256, 5 s signature window), weight-based rate limiter, honours `X-RATE-LIMIT-RESET`, re-signs every retry, maps errors |
| | `infrastructure/exchange/ws.py` | WebSocket client: auth, `enable_heartbeat`, stale-socket watchdog, exponential reconnect + re-subscribe + `on_reconnect` hook |
| | `infrastructure/exchange/instruments.py` | Contract specs from `/v2/products`: tick size, contract value, IM/MM, max leverage, fees, position limits |
| Data | `infrastructure/data/market_data.py` | Live state: ticker (last, mark, index, bid/ask, funding, OI), L2 book, candles, per-feed freshness |
| | `infrastructure/data/history.py` | Paged `/v2/history/candles` download plus CSV cache. Synthetic generator for offline tests only |
| Strategies | `strategies/*.py` | `ema_trend_scalp`, `vwap_reversion`, `bb_squeeze_breakout`, `liquidity_sweep`. All are long/short, configurable, ATR stops, R-multiple targets, time stops |
| Risk | `layers/risk/` | Sizing (risk / margin / exposure / exchange-limit bound), liquidation maths, 20 rules (CR-01…CR-20), persistent kill switch and daily-loss state |
| Engines | `engines/backtester/` | Bar replay: next-bar-open fills, stop-before-target, gap fills, liquidation, funding, fees + GST. Plus a validation gate (full + out-of-sample) |
| | `engines/paper_executor/` | Paper exchange on live data: L2 book walk, maker/taker, partial fills, mark-triggered stops, OCO brackets, funding, liquidation, persistence |
| | `engines/live_executor/` | Real orders: leverage setup, exchange-side bracket SL/TP, idempotent `client_order_id`, ambiguous-failure lookup, partial fills via WS, reconciliation that re-protects positions |
| Automation | `layers/automation/trader.py` | Real-time loop shared by paper and live, with watchdogs |
| | `layers/automation/orchestrator.py` | Command-line interface (`python -m main …`) |

---

## 2. Delta Exchange India facts the code relies on

Checked against the official API docs ([docs.delta.exchange](https://docs.delta.exchange/)), the Delta India API guide, and [delta.exchange/fees](https://www.delta.exchange/fees). **Re-check before going live; exchanges change these.**

| Topic | Value used | Where it's handled |
|---|---|---|
| REST base URL | `https://api.india.delta.exchange`. Testnet: `https://cdn-ind.testnet.deltaex.org`. India keys work only on the India production URL; demo keys only on testnet. `api.delta.exchange` is Delta *Global* and won't accept India keys | `settings.py` |
| WebSocket URL | `wss://socket.india.delta.exchange`. Testnet: `wss://socket-ind.testnet.deltaex.org`. Override with `DELTA_WS_URL` | `settings.py` |
| Auth | Headers `api-key`, `timestamp` (unix seconds), `signature` = HMAC-SHA256(secret, `METHOD + timestamp + path + ?query + body`). Signatures expire after about 5 s. A `User-Agent` header is required | `rest.py` |
| WebSocket auth | `{"type":"auth","payload":{"api-key","signature","timestamp"}}`, signing `GET + timestamp + /live` | `ws.py` |
| Key permissions | Trading keys **must have an IP whitelist**. The error codes refer to "Read Data" and "Trading" permissions. Enable both | `python -m main check` |
| Rate limits | 20,000 weight units per fixed 5-minute window per key. Weights: 3 for reads, 5 for order place/edit/cancel, 10 for fills/history, 25 for batch. 500 ops/s per product. 429 responses include `X-RATE-LIMIT-RESET` (ms) | `rest.py` |
| Margins | `/v2/products` returns `initial_margin` and `maintenance_margin` as **percent** (e.g. BTCUSD `"0.5"` = 0.5 % → 200× max) | `ContractSpec.from_api` |
| Contract size | Linear, USD-quoted. BTCUSD: 1 contract = 0.001 BTC, tick 0.5. Orders are whole contracts | `ContractSpec` |
| Fees | Read live per product (`maker_commission_rate`, `taker_commission_rate`; the docs' BTCUSD sample shows 0.02 % / 0.05 %), **plus 18 % GST** on trading fees. Charged on notional. Option fees are capped at a % of premium | `engines/costs.py` |
| Funding | Perpetuals pay or receive funding on notional; positive rate means longs pay. The ticker's `funding_rate` is in percent. The interval comes from the product (`rate_exchange_interval`, 8 h in the docs sample) | `costs.py`, paper executor, backtester |
| Order API | `POST /v2/orders` takes `client_order_id`, `reduce_only`, `post_only`, `time_in_force` (gtc/ioc), bracket fields (`bracket_stop_loss_price`, `bracket_take_profit_price`, `bracket_stop_trigger_method`). Also `PUT /v2/orders` (edit), `DELETE /v2/orders`, `DELETE /v2/orders/all`, `POST /v2/orders/bracket`, `POST /v2/products/{id}/orders/leverage` | `live_executor` |
| Entity / eligibility | Delta Exchange India is run by Excelium Technologies Pvt Ltd, an FIU-IND registered entity. Account KYC is required. The India platform offers crypto **futures and options**; check `python -m main instruments` for what is live today | — |

**Not confirmed in the official docs during this build, so verify yourself:**

- the exact WebSocket channel names. The code uses `v2/ticker`, `l2_orderbook`, `candlestick_<res>`, `mark_price`, `orders`, `positions`, `v2/user_trades`.
- the current fee table on the India site.
- whether your account settles in USD or INR. The code reads `settling_asset` from the product.
- the funding interval for each product.

---

## 3. Install

Requires Python ≥ 3.10.

```bash
git clone https://github.com/aditya76792/delta-crypto-trader.git
cd delta-crypto-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then edit .env — never commit it
make test                     # 100 offline tests
```

## 4. Configuration

* **Secrets** go only in environment variables or `.env`, which is git-ignored:

  | Variable | Meaning |
  |---|---|
  | `DELTA_ENV` | `prod` or `testnet` |
  | `DELTA_API_KEY`, `DELTA_API_SECRET` | API credentials (not needed for backtest/paper) |
  | `TRADING_MODE` | `backtest`, `paper` or `live` |
  | `DELTA_LIVE_CONFIRM` | Must equal `I_UNDERSTAND_THIS_USES_REAL_MONEY` to allow live |
  | `DELTA_LIVE_NONINTERACTIVE` | `1` only for supervised servers (skips the typed prompt) |
  | `TRADER_SYMBOLS`, `TRADER_STRATEGY`, `TRADER_RESOLUTION`, `TRADER_LEVERAGE` | Overrides |
  | `RISK_MAX_DAILY_LOSS_PCT`, `RISK_PER_TRADE_PCT`, `RISK_MAX_LEVERAGE` | Risk overrides |
  | `PAPER_STARTING_EQUITY_USD` | Paper account size |

* **Everything else** lives in `config/config.yaml`: symbols, resolution, strategy and its parameters, leverage, and all risk limits.
* Secrets are scrubbed from logs: `api_key`, `secret`, `signature` and `token` fields print as `***`.

### Creating API keys (Delta India)

1. Log in → Account → API Keys → Create.
2. Grant **Read Data**. Add **Trading** only when you are ready for live.
3. Add your machine's public IP to the whitelist. This is required for Trading keys.
4. Put the key and secret in `.env`, then run `python -m main check`.

## 5. Workflow: backtest → paper → live

```bash
# 0. Inspect live contract specs, fees, margins, limits (public)
python -m main instruments --symbol BTCUSD ETHUSD

# 1. Download real exchange history and backtest
python -m main fetch-history --days 120
python -m main backtest --all --days 120          # compare strategies
python -m main backtest --validate --days 120     # writes the live-gate report(s)

# 2. Offline end-to-end check of the full pipeline (no network needed)
python -m main replay --synthetic --offline --days 7

# 3. Paper trade on the live Delta India WebSocket feed
python -m main paper                 # Ctrl-C to stop; state persists in data/paper_state.json
python -m main paper --reset         # start over with a fresh paper balance
python -m main status

# 4. Live (real money) — only after paper results look right
export TRADING_MODE=live DELTA_LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY
python -m main check
python -m main live --confirm-live   # then type the confirmation text shown
```

Backtest assumptions you can tune: `--slippage-bps` (default 3) and `--funding-rate` (fraction per 8 h, default 0.0001). Synthetic data (`--synthetic`) only demonstrates the mechanics and **can never pass the gate**.

### Validation gate (required before live)

`backtest --validate` runs the full period plus a 30 % out-of-sample segment and writes `data/backtests/<strategy>_<symbol>_<res>.json`. A report passes only with all of the following:

* at least 30 trades,
* profit factor ≥ 1.1,
* max drawdown ≤ 25 %,
* positive expectancy in R,
* zero liquidations,
* the out-of-sample segment also passes,
* the data came from the exchange.

Live mode refuses to start without a passing report less than 14 days old. Passing is a minimum check. It does not mean the strategy is profitable.

## 6. Live-mode safety gates

`python -m main live` starts only if **every** check passes:

1. `TRADING_MODE=live`
2. `DELTA_LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY`
3. the `--confirm-live` flag, and you type `LIVE <SYMBOLS>` at the prompt
4. credentials exist and an authenticated wallet call works (this proves the key, permissions and IP whitelist)
5. instrument specs were loaded from the live API, not a cache
6. a passing backtest report exists for each symbol
7. the kill switch is off

`LiveExecutor` cannot be created any other way. Paper and live are separate classes with separate state directories (`data/paper/` and `data/live/`).

## 7. Risk controls

| Rule | Check |
|---|---|
| CR-01 | Stop-loss defined and on the protective side |
| CR-02 | Size is a whole number of contracts within the product's position limit |
| CR-03 | Per-trade risk, including round-trip fees + GST, ≤ `risk_per_trade_pct` of equity |
| CR-04 | Daily loss for the UTC day (equity-based, so it includes unrealised P&L, fees and funding). A breach engages the **kill switch and flattens** positions |
| CR-05 | Max concurrent positions |
| CR-06 / CR-07 | Per-symbol and total notional exposure caps (× equity) |
| CR-08 | Leverage ≤ the configured cap and ≤ the exchange max |
| CR-09 | The stop must sit inside 50 % of the entry→liquidation distance and ≥ 3 ATR before liquidation |
| CR-10 | Minimum reward:risk |
| CR-11 | Max bid/ask spread |
| CR-12 | **Stale or missing data blocks entries.** Missing data counts as a failure |
| CR-13 | Last price within 0.5 % of mark (protects against wicks and manipulation) |
| CR-14 | Funding rate not excessively against the trade |
| CR-15 | Instrument state is `live` |
| CR-16 | Prices aligned to the tick size |
| CR-17 | No adding to a losing position |
| CR-18 | Kill switch not engaged |
| CR-19 | Cool-down after N consecutive losses |
| CR-20 | Order-rate throttle |

Two more checks run before any order is planned:

* the stop distance must be at least 2× the round-trip fee, otherwise fees eat the edge;
* size is the minimum of four limits: the risk budget, available margin × leverage, the exposure caps, and the exchange position limit.

Exits (reduce-only) are never blocked by risk rules.

**Kill switch:** run `python -m main kill`, or `touch data/paper/KILL_SWITCH`. Running traders then flatten their positions and stop opening new ones. `python -m main kill --flatten` (with `TRADING_MODE=live`) also cancels all orders and closes positions directly through the API. Release it with `python -m main kill --release`.

## 8. Live execution reliability

* **Duplicate orders.** Each plan gets a deterministic `client_order_id`, and there is a per-symbol in-flight lock. A plan that was already sent is refused. After an ambiguous network or 5xx failure, the executor looks the order up by `client_order_id` (open orders, then fills) before deciding whether to resend.
* **Protection survives crashes.** The SL and TP are placed on the exchange as bracket orders, triggered by mark price. Reconciliation runs at startup, every 60 s, and after every WebSocket reconnect. It treats the exchange as the source of truth, and if it finds an unprotected position it re-attaches a bracket or flattens the position.
* **Partial fills.** These are tracked from the private `orders` and `positions` channels.
* **Rate limits.** A client-side weight budget throttles requests before the exchange's limit. On a 429 the client waits for the reset time. Cancellations are not rate-limited by the exchange.
* **Disconnects.** A watchdog closes a silent socket. Reconnects use exponential backoff with jitter, then re-auth and re-subscribe.

## 9. Testing

```bash
make test       # 100 tests: signing, retries/429, WS reconnect, sizing, every risk rule,
                # backtester edge cases, paper matching, live gates/idempotency/reconcile,
                # end-to-end replay, CLI
make lint
```

## 10. Project layout

```
config/config.yaml            non-secret config
infrastructure/               exchange clients, data, models, settings, logging, event bus
layers/technical/             indicators (+ rolling VWAP, Keltner, Supertrend, realised vol)
layers/risk/                  sizing, rules, checker, kill-switch state
layers/planning.py            signal → TradePlan (shared by backtest/paper/live)
layers/automation/            trader loop, replay feed, CLI
strategies/                   crypto strategies
engines/                      costs, backtester, paper executor, live executor
tests/                        offline test-suite
```
