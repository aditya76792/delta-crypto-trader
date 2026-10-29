---
name: delta-budget
description: >-
  Sizes a Delta Exchange India crypto trade in whole contracts so the loss at the stop INCLUDING fees + GST stays within max_loss_per_trade_usd, bounded by margin x leverage, exposure caps and the exchange position limit, and writes the exact /v2/orders body with bracket stop and target to trader/orders.json. Use for "how many contracts", "size this", "position size".
---

# delta-budget

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main budget
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main budget --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main budget --limit 63950 --write
```

Proposals only: nothing is sent. Next: delta-risk, then delta-broker.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
