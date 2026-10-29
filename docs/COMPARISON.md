# Reverse-engineering and gap analysis

This compares the reference **`day-trader-skill`** (India edition) with **`delta-crypto-trader`**. It also records what was ported, what was rebuilt for crypto, and what was deliberately left out.

## 1. The two codebases

| | `day-trader-skill` (reference) | `delta-crypto-trader` before this change | `delta-crypto-trader` now |
|---|---|---|---|
| Form | Claude **skill pack**: 14 folders, each with `SKILL.md` + one stdlib Python CLI | Python package + CLI (`python -m main`) | Package + CLI **and** a skill pack (`skills/*/SKILL.md`, `.claude-plugin/`) |
| Market | NSE/BSE equities and F&O, IST sessions | Delta Exchange India crypto perpetuals | Same, plus BTC/ETH option-chain analytics |
| Data | Yahoo chart API, NSE/BSE web APIs, Upstox REST polling | Delta REST + WebSocket (pre-docs-check format) | Delta REST + **both** official WebSocket endpoints, in the current message formats |
| Glue | JSON files in `trader/` (each skill reads the last one's output) | in-memory event bus | Both: the event bus for the real-time engine, `trader/*.json` contracts for the desk |
| Automation | `trader-live` engine: paper first; live after validate + paper session + typed approval + a per-entry yes | Real-time trader with 7 live gates | Real-time trader with 8 gates, including the reference's approval process |

## 2. Workflow-by-workflow mapping

| Reference skill | What it does there | Gap in delta-crypto-trader | Crypto-native implementation now |
|---|---|---|---|
| `trader-market` | Sessions, holidays, expiries, lots, ticks, circuits, charges, tax | Specs and costs existed only inside the engine | `desk/market.py` + `clock`, `costs`, `instruments`. Covers the 24/7 clock, next funding time, UTC risk-day reset, fees + 18 % GST, funding, break-even move, margin and liquidation. No holidays, sessions or circuit limits. |
| `trader-scanner` | Universe, daily setups, gaps, ORB, NR7, volume surge, liquidity in ₹ turnover | **Missing** | `desk/scanner.py`: one `/v2/tickers` call for liquidity (24 h USD turnover), spread, OI, funding and 24 h change, then candle setups. Setups: `range_breakout`, `utc_range` (first-hours UTC range, the crypto analogue of the ORB), `momentum`, `squeeze`, `volume_surge`, `funding_extreme`, `liquidity_sweep`. |
| `trader-entry` | Bar-close trigger for orb / level / vwap_reclaim / gap; status triggered/waiting/expired | **Missing** (only strategy signals) | `desk/entry.py`: `range_breakout`, `utc_range`, `level`, `vwap_reclaim` (rolling 24 h VWAP), `strategy:<id>`. A trigger counts only on a **closed** bar, and "expired" means don't chase. Crypto guards: spread, funding against the side, mark/last divergence. |
| `trader-exit` | Stop/target rules, tick rounding away/toward entry, net R:R after charges, flat-by | Stop/target were strategy-internal | `desk/exit_plan.py`: `range-mid / range-low / atr:K / pct:X / price:X / swing:N / vwap`, `NR / price / pct` targets, the same rounding discipline, and costs including GST + funding. **Square-off becomes a time stop.** The plan is refused if the stop is not safely inside the liquidation distance. |
| `trader-timemachine` | 10,000 Monte-Carlo replays from session-slot bar pools + history + backtest | Only a strategy backtester | `desk/timemachine.py`: pool keyed by **UTC time-of-day slot** (crypto has intraday seasonality but no sessions). Checks stop before target, gap fills, the liquidation outcome, and net R after fees/GST/slippage/funding. Also replays the plan at the same UTC time on every past day. The strategy backtester and validation gate were kept. |
| `trader-fno` | Option chain, OI/PCR/max pain/IV, option-buying plans | **Missing** | `desk/options.py`: BTC/ETH chain from `/v2/tickers` with ATM IV, straddle and implied move, PCR, max pain and OI walls. **Analysis only**: option trading is off by default. |
| `trader-budget` | Shares/lots so the loss at the stop incl. charges fits the max loss; margin | Sizing existed inside the engine only | `desk/budget.py`: whole **contracts**, loss at the stop incl. fees+GST ≤ `max_loss_per_trade_usd`, bounded by margin × leverage, exposure caps and the exchange position limit. Writes the exact Delta `/v2/orders` body with bracket SL/TP. |
| `trader-risk` | PASS/BLOCK per order, fingerprints, batch-aware, exits always allowed | Rules existed (CR-01…20) but only for the automated loop | `desk/risk_check.py`: the **same** CR rules + desk limits (allowed/blocked symbols, options switch, trades/orders per UTC day, daily loss in USD), batch-aware, with fingerprinted verdicts in `trader/state/risk.json`. |
| `trader-broker` | Tickets, strict "yes" parsing per order, decision log, paper broker with contract notes | **Missing** (no human-in-the-loop path) | `desk/broker.py`: tickets with the exact Delta body. The yes parser was ported (refuses ok / sure / "yes to all" / conditions / wrong symbol or size). `place` goes to the paper exchange by default, or live only through the live gates. The decision log lives in `trader/state/broker.json`; the paper ledger is `trader/paper/fills.jsonl`. |
| `trader-alerts` | Stop / target / square-off / expiry / circuit / gap alerts | **Missing** | `desk/alerts.py`: above / below / stop / target (side-aware), pct_move, **funding**, **funding_soon**, **liq_distance**, **mark_divergence**, spread, **oi_change**, time. Rings once, can re-arm, and sends a macOS notification. |
| `trader-news`, `trader-analyst`, `trader-insider` | NSE/BSE filings, XBRL results, SEBI insider and bulk deals | n/a | **Not ported**: equity-only data with no official crypto equivalent on Delta. The crypto "context" (funding, OI change, basis via mark vs index, 24 h moves) lives in the scanner and alerts instead. |
| `trader-live` | Upstox polling engine; validate (scripted day); paper session report; `approve-live` typed phrase tied to a limits hash; per-entry `confirm N`; kill; doctor | Real-time engine existed; **missing** validate / paper-session proof / typed approval / per-entry yes / doctor | `layers/automation/live_process.py` + CLI: `validate` (9 scripted scenarios with invariants), paper session reports, `approve-live` / `revoke-live` (limits-hash bound, TTY only), `confirm ID` (a typed yes per live entry, re-planned on fresh data), `doctor`. |

## 3. Problems found in the existing repo, and how they were fixed

These were found by re-checking the official API docs (Websocket Feed section and REST reference, 2026-10-29):

1. **WebSocket auth type**: we sent `{"type":"auth"}`. The docs say that method stopped working after 31-Dec-2025, so we now send `{"type":"key-auth"}` with an integer timestamp and handle the `key-auth` success/failure responses.
2. **Public market data endpoint and format**: market data now lives on `wss://public-socket.india.delta.exchange` with **renamed channels** (`ticker`, `ob_l2`, `trades`) and **compact keys** (`sy`, `ts`, `d[].q`, `a`/`b` arrays, candle `o/h/l/c/v`). The parser now handles both the compact and the legacy formats.
3. **Private payload fields**: `orders` messages carry `order_id` (not `id`) and `symbol`, and the first message after subscribing is a `snapshot`. These are now mapped, so partial fills are tracked correctly.
4. **Funding** comes from its own `funding_rate` channel (`fr` in percent, with `nfr` next-funding time); the new ticker no longer carries it. This feeds the funding risk rule, alerts and the paper funding charge.
5. **Candle closing** no longer depends on the WS candle stream alone. A REST `/v2/history/candles` poller closes every finished bar exactly once.
6. **Paper fills could use a stale order book** after a fast move (found by the new `validate` gap scenario). The book is now used only when it is as fresh as the quote and agrees with it.
7. **Equity** uses the wallet's `meta.net_equity` (it includes unrealised P&L) when the API provides it.
8. **Backtests** use real historical funding (`/v2/history/candles?symbol=FUNDING:<SYM>`) when online.
9. **Dead man's switch** (`/v2/heartbeat/create`, `/v2/heartbeat`) is exposed in the REST client but **off by default**: the docs don't define the TTL units.
