---
name: delta-entry
description: >-
  Entry timer for a crypto setup on Delta Exchange India: waits for a CLOSED bar beyond the level (range breakout, UTC range, level, VWAP reclaim, or any strategy signal) and reports triggered / waiting / expired, with spread, funding and mark-vs-last warnings. Writes trader/entry.json. Use when the user says "tell me when to get in", "has BTC broken out", "watch ETHUSD", "check my watchlist".
---

# delta-entry

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main entry check BTCUSD --setup range_breakout --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main entry check ETHUSD --setup utc_range --side sell
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main entry check BTCUSD --setup strategy:ema_trend_scalp --side auto --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main entry watch BTCUSD --setup level --level 65000
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main entry watchlist
```

"expired" means the trigger happened bars ago: do not chase. It never places an order. Next step: delta-exit.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
