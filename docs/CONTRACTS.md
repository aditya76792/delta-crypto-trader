# File contracts (`trader/` workspace)

The desk tools chain through JSON files in `trader/`. Set `TRADER_WORKSPACE` to move it. Times are UTC ISO-8601 and money is USD. Symbols are Delta product symbols (`BTCUSD`, `ETHUSD`). Writes are atomic.

```
scan → entry → exit → timemachine → budget → risk → broker (paper by default)
alerts (while a position is open)            options (analysis only)
```

| File | Written by | Shape (key fields) |
|---|---|---|
| `config.json` | you | Overrides of `desk.common.DESK_DEFAULTS`. See `config/desk.example.json`. |
| `watchlist.json` | `scan` | `{created_at, resolution, universe, skipped:{SYM:reason}, context:{btc_change_24h_pct, median_funding_rate}, hits:[{symbol, setup, side, price, level, range?, note, turnover_usd_24h, funding_rate, spread_pct, atr_pct, volume_x, bar_close_utc}]}` |
| `entry.json` | `entry check --write` (when triggered) | `{symbol, setup, side, status: triggered\|waiting\|expired\|no-data\|refused, price, level, bar_index, bars_ago, context:{range? vwap?}, suggested_stop?, suggested_target?, warnings[], last_bar_open_utc, resolution}` |
| `plan.json` | `exit plan --write` | `{symbol, product_id, side, entry, stop, stop_rule, target, target_rule, tick, risk_per_contract, reward_risk, net_reward_risk, ref_contracts, costs:{fee_in, fee_stop, fee_target, gst_rate, funding_est, funding_rate}, leverage, liquidation_price_est, stop_share_of_liq_distance, margin_ref, max_hold_hours, warnings[]}` |
| `timemachine.json` | `timemachine --write` | `{plan, data, replays, p_target, p_stop, p_liquidation, p_time, r_at_target, avg_R_gross, net_expectancy_usd, net_expectancy_r, history:{days, target, stop, time, win_rate, avg_R_gross}, verdict}` |
| `orders.json` | `budget --write` / `broker flatten` | `{status:"proposed", paper, orders:[{symbol, product_id, side, size (contracts), order_type, limit_price, leverage, stop_loss, take_profit, reduce_only, client_order_id (≤32), delta_body (exact /v2/orders body), plan, costs:{loss_at_stop_with_fees, net_gain_at_target}, margin, liquidation_price_est, binding_constraint, why}]}` |
| `state/risk.json` | `risk` | `{checks:[{fingerprint, verdict: PASS\|BLOCK, reasons[], day, at}]}`. Fingerprint = sha1 of `symbol, side, size, order_type, limit_price, leverage, stop_loss, take_profit, reduce_only`. |
| `state/broker.json` | `broker` | Append-only `{log:[{event: approved\|declined\|placed\|failed, at, day, fingerprint, symbol, side, size, client_order_id, reply?, mode?, state?, filled?, avg_price?, exchange_id?}]}` |
| `paper/state.json` | paper exchange | Balance, open positions, open orders, seen client order ids (duplicate protection across restarts). |
| `paper/fills.jsonl` | paper exchange | One line per fill `{ts, client_order_id, symbol, side, size, price, fee, maker, realised, reduce_only}` or funding `{ts, symbol, funding, rate}`. |
| `alerts.json` / `alerts.log.jsonl` | `alerts` | Rules (with `fired_at`) and every alert that rang. |
| `prices.json` | `alerts quote --write` | `{quotes:{SYM:{last, mark, funding_rate_pct, change_24h_pct}}}` |
| `options/chain-<ASSET>-<DDMMYY>.json` | `options` | `{spot, expiry, strikes:[{strike, CE:{symbol, mark, bid, ask, iv, oi, delta}, PE:{…}}], analysis:{atm_strike, atm_iv, straddle, implied_move_pct, pcr_oi, max_pain, call_oi_walls, put_oi_walls}}` |
| `live/reports/*-validate.json` | `validate` | `{passed, scenarios:[{name, passed, violations[]}]}` |
| `live/reports/*-paper.json` | `paper` (on exit) | `{feed, hours, errors, ws_reconnects, decisions, equity_start, equity_end, ok}` |
| `live/LIVE_APPROVAL.json` | `approve-live` | `{approved_at, limits_hash, limits}` |
| `live/pending/<id>.json`, `live/confirmed/<id>.json` | live engine / `confirm` | Entries waiting for your typed yes, and your answers. |

The engine's own state (risk day, kill switch, backtest reports, instrument cache) lives in `data/`, as before.
