---
name: delta-market
description: >-
  Market rules for Delta Exchange India crypto derivatives: the 24/7 clock, next funding times, the UTC risk-day reset, contract specs (tick, contract value, margins, max leverage, position limits, fees) and the full cost of a trade (maker/taker fee + 18% GST, funding, slippage, break-even move, margin, estimated liquidation). Use when the user asks "what does this trade cost", "when is funding", "what leverage can I use on BTCUSD", "contract size", "fees on Delta".
---

# delta-market

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main clock
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main instruments --symbol BTCUSD ETHUSD
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main costs --symbol BTCUSD --side buy --entry 60000 --exit 60600 --contracts 10 --leverage 5 --hold-hours 8
```

Every other desk skill relies on these numbers. Fees and margins are read from the live `/v2/products` API (cached in `data/instruments.json`). Facts and their sources: `docs/DELTA_RULES.md`.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
