---
name: delta-options
description: >-
  BTC/ETH option chain analytics on Delta Exchange India (analysis only): ATM IV, ATM straddle and implied move, put/call OI ratio, max pain, call/put OI walls. Writes trader/options/chain-<ASSET>-<EXPIRY>.json. Use for "BTC options", "implied move", "max pain", "PCR".
---

# delta-options

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main options --asset BTC --expiry 311226
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main options --asset ETH --json
```

Option trading stays disabled (allow_options=false). Option fees: 0.01% of notional capped at 3.5% of premium.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
