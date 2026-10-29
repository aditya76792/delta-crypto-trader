# Known limitations

- **Not run against the live exchange from the build environment.** Every API shape comes from the official docs and is exercised in tests with recorded or mocked payloads. Do the first runs on **testnet**.
- **Unverified details** (marked in code):
  - the units of `FUNDING:<SYM>` candle values (assumed percent per interval)
  - the exact REST `/v2/l2orderbook` and option-ticker field names (both shapes are parsed)
  - the TTL units of the dead man's switch, which is why it is off by default
- **Liquidation price** is an isolated-margin estimate: entry × (1 ∓ 1/L ± MMR). In live mode the exchange's `liquidation_price` from the positions channel is shown; trust that one.
- **Paper fills are kinder than real ones.** No queue position, a simple partial-fill model for resting limits (participation in visible size), and no self-trade prevention or ADL.
- **Monte-Carlo replays** resample recent bars by UTC time slot. They reflect the recent regime only, and regimes change quickly in crypto.
- **Options** are analysis only. Execution, Greeks-based sizing and expiry settlement are not implemented.
- **News, earnings and insider workflows** from the equity reference were not ported. There is no official crypto equivalent on Delta.
- **Tax**: none computed. Indian VDA rules are specialised; consult a CA.
- **Not financial advice.** Most leveraged retail traders lose money.
