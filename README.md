# Delta Crypto Trader

A crypto-native trading system for **Delta Exchange India**. It has two ways to work, built on the same engine:

1. **The desk.** This is a skill-style manual workflow ported from [`day-trader-skill`](https://github.com/aditya76792/day-trader-skill):
   `scan → entry → exit → timemachine → budget → risk → broker`. Each step writes a JSON file the next one reads, and **nothing is sent without your explicit yes**. Every step is also a Claude skill (`skills/`).
2. **The real-time engine.** It runs strategies on the live Delta WebSocket feed, with paper trading by default. Live trading is possible only after `validate`, a real-time paper session, your typed `approve-live`, and a typed yes for every entry.

> ⚠️ **Not financial advice. Real money risk.** Leveraged crypto derivatives can lose more than you expect, very quickly. There is no warranty. Start on testnet and paper.

How the two repos compare, what was ported, and what was redesigned for crypto: **[docs/COMPARISON.md](docs/COMPARISON.md)**. The verified exchange facts are in **[docs/DELTA_RULES.md](docs/DELTA_RULES.md)**.

---

## Install

```bash
git clone https://github.com/aditya76792/delta-crypto-trader.git
cd delta-crypto-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                 # API keys only for live; never commit .env
make test                            # offline test suite
```

To use it as a Claude Code plugin:

```
/plugin marketplace add aditya76792/delta-crypto-trader
/plugin install delta-crypto-trader@delta-crypto-trader
```

Python ≥ 3.10. The dependencies are pandas, numpy, httpx, websockets and PyYAML.

## The desk (manual workflow, paper by default)

| Command | Skill | Job |
|---|---|---|
| `clock`, `instruments`, `costs` | `delta-market` | 24/7 clock, next funding, contract specs, full trade cost (fees + 18 % GST, funding, margin, liquidation) |
| `scan` | `delta-scanner` | Liquid perpetuals; range / UTC-range breakouts, momentum, squeezes, volume surges, funding extremes, liquidity sweeps → `trader/watchlist.json` |
| `entry check\|watch\|watchlist` | `delta-entry` | Closed-bar trigger: triggered / waiting / expired, with spread, funding and mark/last warnings → `trader/entry.json` |
| `exit plan\|show` | `delta-exit` | Stop/target rounded to the tick, time stop, leverage, liquidation check, net R:R after costs → `trader/plan.json` |
| `timemachine`, `backtest` | `delta-timemachine` | 10,000 replays from UTC-slot bar pools plus history at the same UTC time; strategy backtests and the live validation gate |
| `budget` | `delta-budget` | Whole contracts so the loss at the stop incl. fees+GST fits `max_loss_per_trade_usd`; exact `/v2/orders` body with bracket SL/TP → `trader/orders.json` |
| `risk` | `delta-risk` | PASS / BLOCK per order (20 crypto rules + desk limits), batch-aware, fingerprinted |
| `broker show\|approve\|decline\|place\|flatten\|log`, `paper-account` | `delta-broker` | Tickets, a strict yes for ONE order, the paper exchange (or live after approval), decision log |
| `alerts add\|check\|watch\|list\|reset\|quote` | `delta-alerts` | Stop/target, % move, funding, funding-soon, liquidation distance, mark divergence, spread, OI change, time |
| `options` | `delta-options` | BTC/ETH chain: ATM IV, straddle / implied move, PCR, max pain, OI walls (analysis only) |

Example (public data, no API key needed):

```bash
python -m main scan
python -m main entry check BTCUSD --setup range_breakout --write
python -m main exit plan --stop atr:1.5 --target 2R --write
python -m main timemachine --write
python -m main budget --write
python -m main risk
python -m main broker show
python -m main broker approve --index 1 --reply "yes"     # your words, verbatim
python -m main broker place --index 1                     # paper exchange
python -m main paper-account status
python -m main alerts add --from-plan && python -m main alerts watch
```

Every desk command accepts `--offline DIR` (saved API JSON, no network) and `--json`. Desk limits live in `trader/config.json` (see `config/desk.example.json`). File formats: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## The real-time engine

```bash
python -m main fetch-history --days 120
python -m main backtest --all --days 120           # compare strategies (real funding history)
python -m main backtest --validate --days 120      # writes the per-symbol live gate
python -m main replay --synthetic --offline --days 7   # offline end-to-end check
python -m main validate                            # 9 scripted safety scenarios
python -m main paper                               # paper trading on the live WebSocket feed
python -m main doctor                              # credentials, IP whitelist, clock, readiness
python -m main approve-live                        # typed by you, bound to your limits
TRADING_MODE=live DELTA_LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY \
  python -m main live --confirm-live               # each entry waits for `confirm ID`
python -m main kill [--flatten] / kill --release   # emergency stop
```

There are four strategies, all long/short with configurable parameters (`config/config.yaml`):

- `ema_trend_scalp`
- `vwap_reversion` (rolling 24 h VWAP)
- `bb_squeeze_breakout`
- `liquidity_sweep`

The full live process and safety table are in **[docs/LIVE.md](docs/LIVE.md)**.

## Architecture

```
infrastructure/
  exchange/  rest.py (signed REST, rate limits, retries, dead-man switch API), ws.py (key-auth,
             heartbeat, reconnect), instruments.py (contract specs)
  data/      market_data.py (compact public-feed + legacy parsing, freshness, REST candle
             fallback), history.py
  workspace.py (trader/ JSON contracts)   models.py   config/   core/ (event bus, logging)
layers/      technical/ (indicators)  risk/ (sizing, liquidation, CR-01…CR-20, kill switch)
             planning.py  automation/ (trader loop, replay, live_process, CLI)
strategies/  crypto scalping & intraday strategies
engines/     costs.py (fees+GST, funding, slippage), backtester/ (+ validation gate),
             paper_executor/ (paper exchange), live_executor/ (gated real orders)
desk/        scanner, entry, exit_plan, timemachine, budget, risk_check, broker, account,
             alerts, options, market, cli
skills/      11 Claude skills (SKILL.md) over the desk + engine
docs/        COMPARISON, DELTA_RULES, CONTRACTS, LIVE, LIMITATIONS
```

## Security

- API keys come **only** from environment variables or `.env` (git-ignored). They are scrubbed from logs.
- Trading keys need an IP whitelist on Delta India.
- Paper is the default everywhere. The live executor can be built only through the gate factory.
- `approve-live` and `confirm` refuse to run without a real terminal.

## Testing

```bash
make test        # unit + end-to-end: signing, retries/429, WS formats & reconnect, sizing, risk rules,
                 # backtester edge cases, paper matching, live gates/idempotency/reconcile,
                 # desk workflow scan→broker, alerts, options, validation scenarios
make lint
```

See also [docs/LIMITATIONS.md](docs/LIMITATIONS.md) and [MIGRATION.md](MIGRATION.md) (the earlier equity→crypto port).
