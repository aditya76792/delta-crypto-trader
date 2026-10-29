# Going live safely

Paper trading is the default. Live orders stay locked until **every** step below is done. The steps follow the reference project's process (validate → real-time paper session → typed approval → a yes per entry), plus crypto-specific gates.

## 1. Credentials (you do this; never paste keys into a chat)
1. Delta Exchange India → API Keys. Create a key with **Read Data**, and add **Trading** only when ready. Trading keys need your machine's public IP on the whitelist.
2. `cp .env.example .env`, then fill in `DELTA_API_KEY` / `DELTA_API_SECRET`. Start with `DELTA_ENV=testnet` and demo-account keys.
3. `python -m main doctor` checks reachability, authentication + IP whitelist, clock offset, readiness and approval status.

## 2. Validate (no network)
```bash
python -m main validate
```
Nine scripted scenarios run through the real Trader + RiskChecker + paper exchange:
- target hit
- stop hit
- short to target
- gap through the stop
- time stop
- entry blocked by stale data
- entry blocked by the kill switch
- daily-loss halt and flatten
- duplicate plan refused

It passes only if:
- every entry was risk-approved
- every filled position had a stop immediately
- the bracket siblings were cancelled (OCO)
- nothing was entered while halted or on stale data
- nothing is left open

## 3. Backtest gate (real history)
```bash
python -m main fetch-history --days 120
python -m main backtest --validate --days 120      # uses real FUNDING:<SYM> history when online
```
Each live symbol needs a passing report (full + out-of-sample, at least 30 trades, PF ≥ 1.1, DD ≤ 25 %, positive expectancy, no liquidations) that is less than 14 days old.

## 4. Paper trade on the real-time feed
```bash
python -m main paper            # Ctrl-C to stop; writes trader/live/reports/<ts>-paper.json
```
You need at least one session of ≥ 4 h with no unhandled errors (`--min-paper-hours` changes this). Run it for days or weeks, honestly.

## 5. Approve (typed by you at a terminal)
```bash
python -m main approve-live
```
This shows your limits and asks for the exact sentence `I APPROVE LIVE TRADING WITH REAL MONEY`. It refuses to run from a script. Changing any limit, symbol, strategy, leverage or resolution afterwards locks live trading until you approve again. To undo: `python -m main revoke-live`.

## 6. Run live
```bash
export TRADING_MODE=live DELTA_LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY
python -m main live --confirm-live           # then type "LIVE BTCUSD,ETHUSD"
```
Every entry signal is written to `trader/live/pending/<id>.json` and waits **120 s** for:
```bash
python -m main confirm <id>                   # type yes
```
Anything else, or no answer, skips it. Once you confirm, the entry is re-planned and re-checked on fresh prices before it is sent.

Exits never wait: the exchange-side bracket stop/target, time stops, kill switch and daily-loss flatten all run without asking.

## Emergency
- `python -m main kill` stops new entries; running engines flatten their positions.
- `TRADING_MODE=live python -m main kill --flatten` cancels all orders and closes every position through the API.
- If the engine is down, the bracket orders are already on the exchange. You can also close positions in the Delta app.

## What protects you
| Risk | Protection |
|---|---|
| Oversized order | Sizing so that the loss at the stop incl. fees + GST fits your limit; the risk check re-checks every order |
| Leverage / liquidation | Leverage cap; the stop must sit inside 50 % of the entry→liquidation distance and ≥ 3 ATR before it |
| Runaway losses | UTC-day equity loss limit halts trading and flattens; cool-down after a loss streak |
| Duplicate orders | Deterministic `client_order_id` per plan; in-flight lock; look-up by `client_order_id` before any resend |
| Crash / disconnect | Exchange-side brackets; reconcile at start, every 60 s and after every reconnect; unprotected positions are re-bracketed or flattened |
| Stale / missing data | No entries while quotes are older than 15 s; a REST candle poller closes bars if the WS stream is silent |
| Accidental live trading | 8 gates: mode, env phrase, CLI flag, credentials + IP, API specs, backtest report, kill switch off, typed approval bound to your limits. Plus a typed yes per entry |
