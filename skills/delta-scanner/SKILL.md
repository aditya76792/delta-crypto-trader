---
name: delta-scanner
description: >-
  Scans Delta Exchange India perpetual futures for crypto setups: liquidity (24h USD turnover, spread), range breakouts on volume, UTC-day range breakouts, momentum, volatility squeezes, volume surges, funding extremes (crowded side) and liquidity sweeps. Writes trader/watchlist.json. Use when the user says "scan crypto", "what's moving", "find setups", "breakouts on Delta", "which perps are liquid".
---

# delta-scanner

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main scan
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main scan --top 20 --lookback 48 --utc-hours 4
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main scan --only BTCUSD,ETHUSD,SOLUSD --json
```

Stage 1 is one public tickers call (no API key). Stage 2 reads closed candles for the most liquid contracts. It never trades. Next step: delta-entry.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
