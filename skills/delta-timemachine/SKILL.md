---
name: delta-timemachine
description: >-
  Replays a planned crypto trade 10,000 times from recent Delta Exchange India bars (bucketed by UTC time-of-day), stop checked before target, gaps and liquidation included, then reports target-first vs stop-first odds and net expectancy in R after fees, GST, slippage and funding, plus how the same plan did at the same UTC time on past days. Use for "is this worth it", "backtest this plan", "what are the odds".
---

# delta-timemachine

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main timemachine --write
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main timemachine --days 60 --replays 20000 --seed 7
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main backtest --all --days 90
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main backtest --validate --days 120
```

Replays reflect the recent regime only. `backtest` runs the automated strategies with the cost-accurate engine; `--validate` writes the report that live trading requires.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
