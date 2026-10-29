---
name: delta-risk
description: >-
  Pre-trade risk check for Delta Exchange India orders: PASS or BLOCK per order in trader/orders.json with reasons (stop side, whole contracts, risk incl. fees, daily loss, positions, exposure, leverage, liquidation distance, reward:risk, spread, stale quote, mark/last divergence, funding against, tick alignment, kill switch, cooldown, trades/orders per day, allowed/blocked symbols). Batch-aware. Use before any order is approved.
---

# delta-risk

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main risk
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main risk --json
```

Exits (reduce-only) are always allowed. Verdicts are stored by fingerprint, so only the exact order that passed can be approved.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
