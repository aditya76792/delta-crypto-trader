"""
Command-line entry point:  python -m main <command> [options]

  instruments      fetch & cache Delta India contract specs (public, no key needed)
  check            verify config, API reachability, credentials, IP whitelist, balances
  fetch-history    download candles for backtesting (public)
  backtest         run strategy backtests (optionally --validate to write the live gate)
  replay           offline end-to-end run: history → WS-shaped feed → Trader → PaperExecutor
  paper            REAL-TIME paper trading on the live Delta India WebSocket feed
  live             REAL-MONEY trading (multiple explicit gates, see README)
  reconcile        compare local vs exchange positions/orders (live credentials)
  kill             engage (or --release) the kill switch; --flatten closes live positions
  status           show risk state and paper account
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from infrastructure.config.settings import Settings, load_settings
from infrastructure.core.events import EventBus
from infrastructure.core.logger import configure_logging, get_logger

log = get_logger("orchestrator")


# ── builders ──────────────────────────────────────────────────────────────────

def _rest(s: Settings, auth: bool = False):
    from infrastructure.exchange.rest import DeltaRestClient
    return DeltaRestClient(s.rest_url, s.api_key if auth else "", s.api_secret if auth else "",
                           user_agent=s.user_agent)


def _registry(s: Settings, online: bool = True):
    from infrastructure.exchange.instruments import InstrumentRegistry
    return InstrumentRegistry.load(_rest(s) if online else None,
                                   s.data_dir / "instruments.json")


def _bars(s: Settings, symbol: str, days: int, synthetic: bool, online: bool = True):
    from infrastructure.data.history import fetch_history, load_cached, synthetic_candles
    cache = s.data_dir / "candles"
    if synthetic:
        return synthetic_candles(bars=days * 288, resolution=s.resolution), "synthetic"
    if online:
        try:
            return fetch_history(_rest(s), symbol, s.resolution, days, cache), "exchange"
        except Exception as exc:
            print(f"! history download failed for {symbol}: {exc}")
    df = load_cached(cache, symbol, s.resolution)
    if df is None or df.empty:
        raise SystemExit(f"No history for {symbol}. Run `python -m main fetch-history` "
                         "with network access, or use --synthetic for an offline demo.")
    return df, "exchange"


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_instruments(s: Settings, a: argparse.Namespace) -> int:
    reg = _registry(s, online=not a.offline)
    print(f"source={reg.source}  instruments={len(reg.all())}")
    print(f"{'symbol':<14}{'type':<20}{'tick':>8}{'ct_val':>10}{'maxLev':>8}{'IM%':>7}"
          f"{'MM%':>7}{'maker':>8}{'taker':>8}{'posLimit':>10}")
    for sp in sorted(reg.all(), key=lambda x: x.symbol):
        if a.symbol and sp.symbol not in a.symbol:
            continue
        print(f"{sp.symbol:<14}{sp.contract_type:<20}{sp.tick_size:>8g}{sp.contract_value:>10g}"
              f"{sp.max_leverage:>8g}{sp.initial_margin * 100:>7.2f}{sp.maintenance_margin * 100:>7.2f}"
              f"{sp.maker_fee * 100:>7.3f}%{sp.taker_fee * 100:>7.3f}%{sp.position_size_limit:>10}")
    return 0


def cmd_check(s: Settings, a: argparse.Namespace) -> int:
    print("config:", json.dumps(s.redacted(), default=str))
    ok = True
    try:
        t = _rest(s).get_ticker(s.symbols[0])
        print(f"✓ public API reachable ({s.rest_url}); {s.symbols[0]} mark={t.get('mark_price')}")
    except Exception as exc:
        ok = False
        print(f"✗ public API unreachable: {exc}")
    if not s.has_credentials:
        print("• no DELTA_API_KEY/DELTA_API_SECRET — fine for backtest/paper, required for live")
        return 0 if ok else 1
    try:
        bals = _rest(s, auth=True).get_balances()
        for b in bals:
            if float(b.get("balance") or 0) > 0:
                print(f"✓ auth OK — {b.get('asset_symbol')}: balance={b.get('balance')} "
                      f"available={b.get('available_balance')}")
        print("✓ key accepted from this IP (Trading keys must be IP-whitelisted)")
    except Exception as exc:
        ok = False
        print(f"✗ authenticated call failed: {exc}\n  check key/secret, prod vs testnet "
              "(DELTA_ENV), key permissions (Read Data + Trading) and the IP whitelist")
    return 0 if ok else 1


def cmd_fetch_history(s: Settings, a: argparse.Namespace) -> int:
    from infrastructure.data.history import fetch_history
    for sym in s.symbols:
        df = fetch_history(_rest(s), sym, s.resolution, a.days, s.data_dir / "candles")
        print(f"{sym} {s.resolution}: {len(df)} bars {df.index.min()} → {df.index.max()}")
    return 0


def cmd_backtest(s: Settings, a: argparse.Namespace) -> int:
    from engines.backtester.engine import Backtester
    from engines.backtester.validation import run_validation
    from strategies import REGISTRY, build_strategy
    reg = _registry(s, online=not a.offline)
    names = list(REGISTRY) if a.all else [s.strategy]
    rc = 0
    for sym in s.symbols:
        bars, source = _bars(s, sym, a.days, a.synthetic, online=not a.offline)
        spec = reg.get(sym)
        for name in names:
            params = s.strategy_params.get(name, {}) if name == s.strategy or a.all else {}
            bt = Backtester(build_strategy(name, params), spec, s.risk,
                            s.paper_starting_equity, s.default_leverage,
                            slippage_bps=a.slippage_bps, funding_rate=a.funding_rate)
            if a.validate:
                rep = run_validation(bt, bars, sym, s.resolution, s.data_dir / "backtests",
                                     data_source=source)
                m = rep["full"]
                verdict = "PASS" if rep["passed"] else "FAIL"
                print(f"[{verdict}] {name:<20} {sym:<8} trades={m['trades']:<4} "
                      f"ret={m['return_pct']}% dd={m['max_drawdown_pct']}% pf={m['profit_factor']} "
                      f"expR={m['expectancy_r']} fees={m['fees']} funding={m['funding']}")
                for f in rep["failures"]:
                    print(f"         - {f}")
                rc |= 0 if rep["passed"] else 2
            else:
                m = bt.run(bars, sym).metrics()
                print(json.dumps(m, default=str))
    if a.synthetic:
        print("\nNOTE: synthetic data — results demonstrate mechanics only and can never pass "
              "the live gate.")
    return rc if a.strict else 0


def _paper_stack(s: Settings, reg: Any, clock: Any = None, state: bool = True):
    import time as _t

    from engines.paper_executor.executor import PaperExecutor
    from infrastructure.data.market_data import MarketDataStore
    from layers.risk.state import RiskState
    clock = clock or _t.time
    store = MarketDataStore(s.resolution, clock=clock)
    ex = PaperExecutor(reg, store, s.paper_starting_equity, s.paper_slippage_bps, clock=clock,
                       state_path=(s.data_dir / "paper_state.json") if state else None)
    rs = RiskState.load(s.data_dir / ("paper" if state else "replay"))
    return store, ex, rs


def cmd_replay(s: Settings, a: argparse.Namespace) -> int:
    from layers.automation.replay import ReplayClock, replay_messages
    from layers.automation.trader import Trader
    from strategies import build_strategy
    reg = _registry(s, online=not a.offline)
    clock = ReplayClock()
    store, ex, rs = _paper_stack(s, reg, clock, state=False)
    rs.release_kill_switch()
    tr = Trader(s, reg, store, ex, build_strategy(s.strategy, s.strategy_params.get(s.strategy)),
                rs, clock=clock)
    for sym in s.symbols:
        bars, source = _bars(s, sym, a.days, a.synthetic, online=not a.offline)
        for msg in replay_messages(bars, sym, s.resolution, clock):
            tr.handle_message(msg)
    kinds: dict[str, int] = {}
    for d in tr.decisions:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    print(json.dumps({"equity": round(ex.equity(), 4), "fills": len(ex.fills),
                      "open_positions": {k: v.size for k, v in ex.positions().items()},
                      "decisions": kinds, "halted": tr.halt_reason or None}, indent=2))
    return 0


def cmd_paper(s: Settings, a: argparse.Namespace) -> int:
    from infrastructure.data.history import fetch_history
    from infrastructure.exchange.ws import DeltaWebSocket
    from layers.automation.trader import Trader
    from strategies import build_strategy
    s.mode = "paper"
    reg = _registry(s)
    store, ex, rs = _paper_stack(s, reg)
    if a.reset:
        ex.reset_state()
        store, ex, rs = _paper_stack(s, reg)
    for sym in s.symbols:
        reg.get(sym)
        store.seed_candles(sym, fetch_history(_rest(s), sym, s.resolution, a.seed_days))
    tr = Trader(s, reg, store, ex, build_strategy(s.strategy, s.strategy_params.get(s.strategy)), rs)
    print(f"PAPER trading {s.symbols} [{s.strategy} {s.resolution}] on live Delta India data. "
          f"equity={ex.equity():.2f} USD. Ctrl-C to stop.")
    ws = DeltaWebSocket(s.ws_url)
    try:
        asyncio.run(tr.run(ws, live=False, stop_after_s=a.duration))
    except KeyboardInterrupt:
        pass
    print(json.dumps({"equity": round(ex.equity(), 4), "positions":
                      {k: v.size for k, v in ex.positions().items()}}, indent=2))
    return 0


def cmd_live(s: Settings, a: argparse.Namespace) -> int:
    from engines.live_executor.factory import LiveGateError, build_live_executor
    from infrastructure.data.history import fetch_history
    from infrastructure.data.market_data import MarketDataStore
    from infrastructure.exchange.ws import DeltaWebSocket
    from layers.automation.trader import Trader
    from layers.risk.state import RiskState
    from strategies import build_strategy
    reg = _registry(s)
    rs = RiskState.load(s.data_dir / "live")
    rest = _rest(s, auth=True)
    try:
        ex = build_live_executor(s, rest, reg, a.confirm_live, s.data_dir / "backtests",
                                 rs.kill_switch_active()[0])
    except LiveGateError as exc:
        print(str(exc))
        return 3
    issues = ex.reconcile()
    if issues:
        print("reconcile issues at startup:", issues)
    store = MarketDataStore(s.resolution)
    for sym in s.symbols:
        store.seed_candles(sym, fetch_history(rest, sym, s.resolution, 5))
    tr = Trader(s, reg, store, ex, build_strategy(s.strategy, s.strategy_params.get(s.strategy)), rs)
    ws = DeltaWebSocket(s.ws_url, s.api_key, s.api_secret)
    print(f"LIVE trading {s.symbols} with REAL MONEY. Kill switch: `python -m main kill`.")
    try:
        asyncio.run(tr.run(ws, live=True, stop_after_s=a.duration))
    except KeyboardInterrupt:
        print("stopped; exchange-side brackets keep protecting open positions")
    return 0


def cmd_reconcile(s: Settings, a: argparse.Namespace) -> int:
    rest = _rest(s, auth=True)
    print(json.dumps({"positions": rest.get_positions(), "open_orders": rest.get_open_orders()},
                     indent=2, default=str))
    return 0


def cmd_kill(s: Settings, a: argparse.Namespace) -> int:
    from layers.risk.state import RiskState
    sub = "live" if s.mode == "live" else "paper"
    rs = RiskState.load(s.data_dir / sub)
    if a.release:
        rs.release_kill_switch()
        print(f"kill switch released ({sub})")
        return 0
    rs.engage_kill_switch(a.reason)
    print(f"KILL SWITCH ENGAGED ({sub}): {a.reason}. Running traders will flatten and stop "
          "opening positions.")
    if a.flatten and s.mode == "live":
        rest = _rest(s, auth=True)
        rest.cancel_all()
        for p in rest.get_positions() or []:
            size = int(float(p.get("size", 0) or 0))
            if size:
                rest.place_order({"product_id": p.get("product_id"), "size": abs(size),
                                  "side": "sell" if size > 0 else "buy",
                                  "order_type": "market_order", "reduce_only": True})
                print(f"flatten sent for {p.get('product_symbol')} size={size}")
    return 0


def cmd_status(s: Settings, a: argparse.Namespace) -> int:
    for sub in ("paper", "live"):
        p = s.data_dir / sub / "risk_state.json"
        k = s.data_dir / sub / "KILL_SWITCH"
        print(f"[{sub}] risk_state={p.read_text() if p.exists() else '-'} "
              f"kill_switch={'ENGAGED' if k.exists() else 'off'}")
    ps = s.data_dir / "paper_state.json"
    if ps.exists():
        d = json.loads(ps.read_text())
        print(f"[paper] balance={d.get('balance')} positions={list(d.get('positions', {}))}")
    return 0


# ── parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m main", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", help="comma list, overrides config (e.g. BTCUSD,ETHUSD)")
    p.add_argument("--strategy", help="strategy id")
    p.add_argument("--resolution", help="candle resolution, e.g. 1m 5m 15m")
    p.add_argument("--env", choices=["prod", "testnet"])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("instruments")
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--symbol", nargs="*")
    sub.add_parser("check")
    sp = sub.add_parser("fetch-history")
    sp.add_argument("--days", type=int, default=90)
    for name in ("backtest", "replay"):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=60)
        sp.add_argument("--synthetic", action="store_true", help="offline synthetic data (demo only)")
        sp.add_argument("--offline", action="store_true")
        if name == "backtest":
            sp.add_argument("--all", action="store_true", help="every registered strategy")
            sp.add_argument("--validate", action="store_true", help="write live-gate report")
            sp.add_argument("--strict", action="store_true", help="non-zero exit on FAIL")
            sp.add_argument("--slippage-bps", type=float, default=3.0)
            sp.add_argument("--funding-rate", type=float, default=0.0001,
                            help="assumed funding per 8h interval (fraction)")
    sp = sub.add_parser("paper")
    sp.add_argument("--duration", type=float, default=None, help="seconds, default forever")
    sp.add_argument("--reset", action="store_true", help="reset paper balance/positions")
    sp.add_argument("--seed-days", type=int, default=5)
    sp = sub.add_parser("live")
    sp.add_argument("--confirm-live", action="store_true")
    sp.add_argument("--duration", type=float, default=None)
    sub.add_parser("reconcile")
    sp = sub.add_parser("kill")
    sp.add_argument("--release", action="store_true")
    sp.add_argument("--flatten", action="store_true")
    sp.add_argument("--reason", default="manual")
    sub.add_parser("status")
    return p


COMMANDS = {
    "instruments": cmd_instruments, "check": cmd_check, "fetch-history": cmd_fetch_history,
    "backtest": cmd_backtest, "replay": cmd_replay, "paper": cmd_paper, "live": cmd_live,
    "reconcile": cmd_reconcile, "kill": cmd_kill, "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import os
    if args.symbols:
        os.environ["TRADER_SYMBOLS"] = args.symbols
    if args.strategy:
        os.environ["TRADER_STRATEGY"] = args.strategy
    if args.resolution:
        os.environ["TRADER_RESOLUTION"] = args.resolution
    if args.env:
        os.environ["DELTA_ENV"] = args.env
    s = load_settings()
    if args.cmd != "live" and s.mode == "live" and args.cmd in ("paper", "replay", "backtest"):
        s.mode = "paper"
    configure_logging(s.log_dir, s.log_level, also_stdout=args.cmd in ("paper", "live"))
    EventBus.reset()
    return COMMANDS[args.cmd](s, args)


if __name__ == "__main__":
    sys.exit(main())
