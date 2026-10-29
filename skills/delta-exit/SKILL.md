---
name: delta-exit
description: >-
  Exit planner for a Delta Exchange India crypto trade BEFORE entering: stop (range-mid, atr:K, pct:X, swing:N, vwap, price) rounded away from entry, target (2R, price, pct) rounded toward entry, leverage, margin, estimated liquidation (refuses stops too close to it), time stop instead of a square-off, and net reward:risk after fees + GST + funding. Writes trader/plan.json. Use for "where's my stop", "plan the exit", "what's my risk/reward".
---

# delta-exit

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main exit plan --stop atr:1.5 --target 2R --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main exit plan --symbol BTCUSD --side buy --entry 64000 --stop swing:12 --target 2.5R --leverage 5 --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main exit show
```

Next step: delta-timemachine (does this plan have an edge after costs?) then delta-budget.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
