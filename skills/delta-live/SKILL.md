---
name: delta-live
description: >-
  Real-time crypto trading engine on Delta Exchange India: paper trading on the live WebSocket feed by default; live only after validate + a real-time paper session + typed approve-live + a typed yes per entry. Exchange-side brackets, reconciliation, duplicate-order prevention, reconnects, daily-loss halt and kill switch. Use for "run the bot", "paper trade live", "go live", "kill switch", "is it ready for live".
---

# delta-live

Part of the Delta Crypto Trader skill pack (crypto-native rebuild of the
day-trader-skill workflow for Delta Exchange India). Run from the repository root
(`${CLAUDE_SKILL_DIR}/../..`). Add `--offline DIR` to work from saved API JSON and
`--json` for machine-readable output.

```bash
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main validate
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main paper
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main status
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main doctor
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main approve-live
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main live --confirm-live
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main confirm <id>
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main kill
cd "${CLAUDE_SKILL_DIR}/../.." && python3 -m main kill --release
```

approve-live and confirm must be typed by the user at a terminal: never run them for the user. See docs/LIVE.md.

Workflow: `scan → entry → exit → timemachine → budget → risk → broker` (paper by default).
Files: see `docs/CONTRACTS.md`. Not financial advice: leveraged crypto trading can lose
more than you expect, quickly.
