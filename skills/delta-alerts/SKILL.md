---
name: delta-alerts
description: >-
  Crypto alerts for Delta Exchange India: stop/target (side-aware, last or mark price), percent moves, funding rate thresholds, funding-in-N-minutes for held positions, liquidation-distance, mark/last divergence, wide spread, open-interest change and UTC time alerts. Use for "alert me if BTC hits", "watch my stop", "tell me before funding", "am I close to liquidation".
---

# delta-alerts

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts add --from-plan
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts add --type above --symbol BTCUSD --price 70000
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts add --type funding --symbol ETHUSD --rate 0.0005
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts check
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts watch --every 30
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts list
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main alerts quote --symbols BTCUSD,ETHUSD --write
```

Alerts ring once (unless --rearm), are logged to trader/alerts.log.jsonl and pop a macOS notification.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
