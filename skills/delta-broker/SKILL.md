---
name: delta-broker
description: >-
  Order executor for Delta Exchange India with a human in the loop: shows each ticket (contracts, leverage, margin, liquidation, bracket stop/target, loss at stop incl. fees, exact API body), records the user's explicit yes for ONE order (refuses ok/sure/yes-to-all/conditional/wrong symbol or size), places it on the paper exchange by default (live only after the live approval process), shows the paper account, builds flatten tickets. Use for "place it", "buy BTC", "show my tickets", "paper account".
---

# delta-broker

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main broker show
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main broker approve --index 1 --reply "yes"
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main broker place --index 1
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main paper-account status
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main paper-account check
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main broker flatten --symbol BTCUSD
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main broker log
```

NEVER approve on the user's behalf: pass their reply word for word. Paper is the default (`paper: true`).

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
