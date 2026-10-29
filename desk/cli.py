"""
CLI for the desk workflow (registered into `python -m main`):

  clock | costs | scan | entry | exit | timemachine | budget | risk | broker |
  paper-account | alerts | options

Every command accepts --offline DIR (saved JSON, no network) and --json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from infrastructure.config.settings import Settings
from infrastructure.workspace import read_json, write_json


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--offline", help="folder with saved API JSON (no network)")
    p.add_argument("--json", action="store_true", help="print raw JSON")


def register(sub: Any) -> None:
    p = sub.add_parser("clock", help="24/7 clock, next funding times, UTC risk-day reset")
    _common(p)
    p = sub.add_parser("costs", help="round-trip cost of a trade (fees + GST, funding, liq)")
    _common(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", choices=["buy", "sell"], default="buy")
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--exit", dest="exit_", type=float, required=True)
    p.add_argument("--contracts", type=int, required=True)
    p.add_argument("--leverage", type=float, default=5)
    p.add_argument("--maker-entry", action="store_true")
    p.add_argument("--maker-exit", action="store_true")
    p.add_argument("--hold-hours", type=float, default=0)
    p.add_argument("--funding-rate", type=float, default=None, help="fraction per interval")
    p.add_argument("--slippage-bps", type=float, default=0)

    p = sub.add_parser("scan", help="scan Delta India perpetuals → trader/watchlist.json")
    _common(p)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--lookback", type=int, default=48)
    p.add_argument("--utc-hours", type=int, default=4)
    p.add_argument("--funding-extreme", type=float, default=0.0005)
    p.add_argument("--only", help="comma list of symbols")
    p.add_argument("--no-write", action="store_true")

    p = sub.add_parser("entry", help="entry timer on closed bars → trader/entry.json")
    _common(p)
    p.add_argument("action", choices=["check", "watch", "watchlist"])
    p.add_argument("symbol", nargs="?")
    p.add_argument("--setup", default="range_breakout",
                   help="range_breakout | utc_range | level | vwap_reclaim | strategy:<id>")
    p.add_argument("--side", choices=["buy", "sell", "auto"], default="buy")
    p.add_argument("--level", type=float)
    p.add_argument("--lookback", type=int, default=48)
    p.add_argument("--utc-hours", type=int, default=4)
    p.add_argument("--max-age", type=int, default=0, help="bars a trigger stays fresh")
    p.add_argument("--max-polls", type=int, default=288)
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("exit", help="stop/target/liquidation/costs plan → trader/plan.json")
    _common(p)
    p.add_argument("action", choices=["plan", "show"])
    p.add_argument("--symbol")
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", help="range-mid | range-low | range-high | atr:K | pct:X | price:X | swing:N | vwap")
    p.add_argument("--target", default="2R", help="2R | 1.5R | price:X | pct:X")
    p.add_argument("--leverage", type=float)
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("timemachine", help="10,000 replays + history of the plan, net of costs")
    _common(p)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--replays", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--window", type=int, default=2)
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("budget", help="size the plan in contracts → trader/orders.json")
    _common(p)
    p.add_argument("--limit", type=float, help="limit price (default: market order)")
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("risk", help="PASS/BLOCK every order in trader/orders.json")
    _common(p)
    p.add_argument("--no-write", action="store_true")

    p = sub.add_parser("broker", help="tickets, your explicit yes, paper/live placement")
    _common(p)
    p.add_argument("action", choices=["show", "approve", "decline", "place", "flatten", "log"])
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--reply", default="")
    p.add_argument("--symbol")
    p.add_argument("--confirm-live", action="store_true")

    p = sub.add_parser("paper-account", help="paper account: status / check / reset")
    _common(p)
    p.add_argument("action", choices=["status", "check", "reset"])
    p.add_argument("--capital", type=float)

    p = sub.add_parser("alerts", help="price, funding, liquidation-distance alerts")
    _common(p)
    p.add_argument("action", choices=["add", "list", "check", "watch", "reset", "quote"])
    p.add_argument("--type")
    p.add_argument("--symbol")
    p.add_argument("--symbols")
    p.add_argument("--price", type=float)
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--pct", type=float)
    p.add_argument("--rate", type=float)
    p.add_argument("--minutes", type=float)
    p.add_argument("--usd", type=float)
    p.add_argument("--at")
    p.add_argument("--on", choices=["last", "mark"], default="last")
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--from-plan", action="store_true")
    p.add_argument("--id")
    p.add_argument("--every", type=float, default=30)
    p.add_argument("--max-loops", type=int, default=0)
    p.add_argument("--write", action="store_true")

    p = sub.add_parser("options", help="BTC/ETH option chain analytics (analysis only)")
    _common(p)
    p.add_argument("--asset", default="BTC")
    p.add_argument("--expiry", help="DDMMYY as in the option symbol (e.g. 310326)")


DESK_COMMANDS = {"clock", "costs", "scan", "entry", "exit", "timemachine", "budget", "risk",
                 "broker", "paper-account", "alerts", "options"}


def _out(a: argparse.Namespace, doc: Any, text: str | None = None) -> None:
    if getattr(a, "json", False) or text is None:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(text)


def _source(s: Settings, a: argparse.Namespace) -> Any:
    from desk.common import Source
    return Source(s, offline=getattr(a, "offline", None))


def dispatch(s: Settings, a: argparse.Namespace) -> int:
    from desk.common import desk_config
    cfg = desk_config()
    src = _source(s, a)
    return globals()["_cmd_" + a.cmd.replace("-", "_")](s, a, cfg, src)


# ── commands ──────────────────────────────────────────────────────────────────

def _cmd_clock(s, a, cfg, src) -> int:
    from desk.market import clock
    tick = {}
    for sym in s.symbols:
        try:
            t = src.ticker(sym)
            tick[sym] = {"funding_rate_pct": t.get("funding_rate"),
                         "next_funding_ts": None}
        except Exception:
            pass
    _out(a, clock(symbols=s.symbols, tickers=tick, hours_utc=cfg["funding_hours_utc"]))
    return 0


def _cmd_costs(s, a, cfg, src) -> int:
    from desk.market import costs
    spec = src.registry().get(a.symbol)
    fr = a.funding_rate
    if fr is None:
        try:
            fr = float(src.ticker(a.symbol).get("funding_rate") or 0) / 100
        except Exception:
            fr = 0.0
    _out(a, costs(spec, a.side, a.entry, a.exit_, a.contracts, a.leverage, a.maker_entry,
                  a.maker_exit, a.hold_hours, fr, a.slippage_bps))
    return 0


def _cmd_scan(s, a, cfg, src) -> int:
    from desk.scanner import scan
    only = [x.strip().upper() for x in a.only.split(",")] if a.only else None
    doc = scan(src, cfg, cfg["resolution"], a.top, only,
               {"lookback": a.lookback, "utc_range_hours": a.utc_hours,
                "funding_extreme": a.funding_extreme}, write=not a.no_write)
    lines = [f"universe {doc['universe']} liquid perpetuals, {len(doc['hits'])} hits "
             f"(context: BTC 24h {doc['context']['btc_change_24h_pct']}%, median funding "
             f"{doc['context']['median_funding_rate']})"]
    for h in doc["hits"]:
        lines.append(f"  {h['symbol']:<10} {h['setup']:<16} {str(h.get('side') or '-'):<5} "
                     f"{h.get('price')}  {h.get('note', '')}")
    lines.append(f"skipped: {len(doc['skipped'])}")
    _out(a, doc, "\n".join(lines))
    return 0


def _entry_opts(a) -> dict[str, Any]:
    return {"lookback": a.lookback, "utc_range_hours": a.utc_hours, "level": a.level,
            "max_age": a.max_age}


def _cmd_entry(s, a, cfg, src) -> int:
    from desk.entry import check
    if a.action == "watchlist":
        wl = read_json("watchlist.json", {}) or {}
        out = []
        for h in wl.get("hits", []):
            if not h.get("side") or h["setup"] in ("squeeze", "funding_extreme", "volume_surge"):
                continue
            setup = h["setup"] if h["setup"] in ("range_breakout", "utc_range") else "range_breakout"
            r = check(src, cfg, h["symbol"], setup, h["side"], cfg["resolution"], _entry_opts(a))
            out.append({"symbol": h["symbol"], "setup": setup, "side": h["side"],
                        "status": r["status"], "price": r.get("price"), "reason": r.get("reason")})
        _out(a, out)
        return 0
    if not a.symbol:
        print("entry check/watch needs a SYMBOL")
        return 1
    if a.action == "check":
        doc = check(src, cfg, a.symbol.upper(), a.setup, a.side, cfg["resolution"], _entry_opts(a),
                    write=a.write)
        _out(a, doc, f"{a.symbol.upper()} {a.setup} {a.side}: {doc['status'].upper()} — "
                     f"{doc.get('reason')} (price {doc.get('price')}, level {doc.get('level')})"
                     + "".join(f"\n  ! {w}" for w in doc.get("warnings", [])))
        return 0
    from infrastructure.data.market_data import RES_SECONDS
    step = RES_SECONDS[cfg["resolution"]]
    for _ in range(a.max_polls):
        doc = check(src, cfg, a.symbol.upper(), a.setup, a.side, cfg["resolution"], _entry_opts(a),
                    write=a.write)
        print(f"{doc.get('last_bar_open_utc')} {doc['status']}: {doc.get('reason')}")
        if doc["status"] in ("triggered", "refused", "no-data") or a.offline:
            return 0
        time.sleep(step - time.time() % step + 3)    # wait for the next bar to CLOSE
    return 0


def _cmd_exit(s, a, cfg, src) -> int:
    from desk.exit_plan import PlanError, build_plan
    if a.action == "show":
        _out(a, read_json("plan.json", {}))
        return 0
    entry_doc = read_json("entry.json", None) if not a.symbol else None
    sym = (a.symbol or (entry_doc or {}).get("symbol") or "").upper()
    if not sym:
        print("no trader/entry.json; pass --symbol --entry --side")
        return 1
    spec = src.registry().get(sym)
    bars = src.candles(sym, cfg["resolution"], 300)
    entry = a.entry
    if entry is None and entry_doc is None:
        entry = float(bars["close"].iloc[-1])
    try:
        fr = float(src.ticker(sym).get("funding_rate") or 0) / 100
    except Exception:
        fr = 0.0
    try:
        plan = build_plan(spec, entry_doc, cfg, a.stop, a.target, bars, entry, a.side, fr,
                          a.leverage, write=a.write)
    except PlanError as exc:
        print(f"PLAN REFUSED: {exc}")
        return 2
    _out(a, plan, (f"{sym} {plan['side']} entry {plan['entry']} stop {plan['stop']} target "
                   f"{plan['target']}  R:R {plan['reward_risk']} (net {plan['net_reward_risk']})  "
                   f"lev {plan['leverage']:g}x liq≈{plan['liquidation_price_est']}  time stop "
                   f"{plan['max_hold_hours']}h" + "".join(f"\n  ! {w}" for w in plan["warnings"])))
    return 0


def _cmd_timemachine(s, a, cfg, src) -> int:
    from desk.timemachine import run
    from infrastructure.data.market_data import RES_SECONDS
    plan = read_json("plan.json")
    if not plan:
        print("no trader/plan.json (run `exit plan --write` first)")
        return 1
    res = plan.get("resolution") or cfg["resolution"]
    bars = src.candles(plan["symbol"], res, a.days * 86400 // RES_SECONDS[res])
    fr = plan.get("costs", {}).get("funding_rate", 0.0)
    doc = run(plan, src.registry().get(plan["symbol"]), bars, cfg, a.replays, a.seed, a.window, fr,
              write=a.write)
    _out(a, doc, f"{doc['verdict']}\n  liquidation {doc['p_liquidation']:.1%}, time stop "
                 f"{doc['p_time']:.1%}; history at the same UTC time: {doc['history']}")
    return 0


def _cmd_budget(s, a, cfg, src) -> int:
    from desk.account import exposure, paper_executor
    from desk.budget import SizeError, size_order, write_orders
    plan = read_json("plan.json")
    if not plan:
        print("no trader/plan.json")
        return 1
    spec = src.registry().get(plan["symbol"])
    if cfg["paper"]:
        ex = paper_executor(src, cfg, [plan["symbol"]])
        eq, av = ex.equity(), ex.available_margin()
        per, tot = exposure(ex, ex.registry, ex.store)
    else:
        from engines.live_executor.executor import LiveExecutor  # noqa: F401
        from infrastructure.exchange.rest import DeltaRestClient
        rest = DeltaRestClient(s.rest_url, s.api_key, s.api_secret, user_agent=s.user_agent)
        bal = rest.get_balances()
        usd = next((b for b in bal if b.get("asset_symbol") == "USD"), {})
        eq = float(rest.last_meta.get("net_equity") or usd.get("balance") or 0)
        av = float(usd.get("available_balance") or 0)
        per, tot = {}, 0.0
    try:
        o = size_order(plan, spec, cfg, eq, av, per.get(plan["symbol"], 0.0), tot,
                       "limit_order" if a.limit else "market_order", a.limit)
    except SizeError as exc:
        print(f"SIZE REFUSED: {exc}")
        return 2
    if a.write:
        write_orders([o], cfg["paper"], f"trader/plan.json ({plan['symbol']} {plan.get('setup')})")
    _out(a, o, f"{o['why']}\n  margin ${o['margin']['required']:.2f} at {o['leverage']:g}x, "
               f"liq≈{o['liquidation_price_est']}" + ("" if a.write else "\n  (dry run: add --write)"))
    return 0


def _paper_state(src, cfg, symbols):
    from desk.account import paper_executor, today_activity
    from infrastructure.workspace import path
    from layers.risk.state import RiskState
    ex = paper_executor(src, cfg, symbols)
    rs = RiskState.load(path("paper"))
    rs.roll_day_if_needed(ex.equity())
    return ex, rs, today_activity()


def _cmd_risk(s, a, cfg, src) -> int:
    from desk.risk_check import check_orders
    doc = read_json("orders.json")
    if not doc or not doc.get("orders"):
        print("no trader/orders.json")
        return 1
    syms = [o["symbol"] for o in doc["orders"]]
    ex, rs, act = _paper_state(src, cfg, syms)
    res = check_orders(doc["orders"], cfg, ex.registry, ex.store, ex.equity(),
                       rs.day_start_equity, ex.positions(), act, rs, write=not a.no_write)
    lines = []
    for r in res["results"]:
        lines.append(f"[{r['verdict']}] #{r['index']} {r['side']} {r['size']} {r['symbol']}"
                     + "".join(f"\n    - {x}" for x in r["reasons"]))
    _out(a, res, "\n".join(lines))
    return 0 if all(r["verdict"] == "PASS" for r in res["results"]) else 2


def _cmd_broker(s, a, cfg, src) -> int:
    from desk import broker as B
    doc = read_json("orders.json", {}) or {}
    orders = doc.get("orders", [])
    try:
        if a.action == "show":
            print("\n".join(B.ticket(o, i, len(orders)) for i, o in enumerate(orders, 1)) or "no orders")
        elif a.action == "approve":
            r = B.approve(orders, a.index, a.reply)
            print(f"recorded: {r['note']}")
        elif a.action == "decline":
            B.decline(orders, a.index, a.reply or "no")
            print("recorded a no")
        elif a.action == "place":
            if cfg["paper"] or s.mode != "live":
                ex, _, _ = _paper_state(src, cfg, [orders[a.index - 1]["symbol"]])
                r = B.place(orders, a.index, ex, "paper")
            else:
                from engines.live_executor.factory import build_live_executor
                from infrastructure.exchange.instruments import InstrumentRegistry
                from infrastructure.exchange.rest import DeltaRestClient
                from layers.risk.state import RiskState
                rest = DeltaRestClient(s.rest_url, s.api_key, s.api_secret, user_agent=s.user_agent)
                reg = InstrumentRegistry.load(rest, s.data_dir / "instruments.json")
                ex = build_live_executor(s, rest, reg, a.confirm_live, s.data_dir / "backtests",
                                         RiskState.load(s.data_dir / "live").kill_switch_active()[0],
                                         interactive=False, prompt=None)
                r = B.place(orders, a.index, ex, "live")
            print(json.dumps(r, indent=2, default=str))
        elif a.action == "flatten":
            ex, _, _ = _paper_state(src, cfg, [a.symbol.upper()])
            pos = ex.position(a.symbol.upper())
            if not pos.is_open:
                print(f"no open {a.symbol} position")
                return 1
            t = B.flatten_ticket(a.symbol.upper(), pos, pos.leverage)
            from desk.budget import write_orders
            write_orders([t], cfg["paper"], f"flatten {a.symbol.upper()}")
            print("flatten ticket written to trader/orders.json — run `risk`, then approve and place")
        elif a.action == "log":
            _out(a, B.todays_log())
    except (B.BrokerError, IndexError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    return 0


def _cmd_paper_account(s, a, cfg, src) -> int:
    from infrastructure.workspace import path
    if a.action == "reset":
        p = path("paper/state.json")
        if p.exists():
            p.rename(p.with_name(f"state-{int(time.time())}.json"))
        if a.capital:
            cfgf = read_json("config.json", {}) or {}
            cfgf["paper_starting_equity_usd"] = a.capital
            write_json("config.json", cfgf)
        print("paper account reset (old state kept as a copy)")
        return 0
    state = read_json(path("paper/state.json"), {}) or {}
    syms = list(state.get("positions", {}).keys()) or s.symbols
    ex, rs, act = _paper_state(src, cfg, syms)
    if a.action == "check":
        for sym in syms:
            ex.on_market(sym)
        ex.save()
    doc = {"equity": round(ex.equity(), 6), "balance": round(ex.balance, 6),
           "available_margin": round(ex.available_margin(), 6),
           "positions": {k: {"size": v.size, "entry": v.entry_price, "stop": v.stop_loss,
                             "target": v.take_profit} for k, v in ex.positions().items()},
           "open_orders": [{"id": o.client_order_id, "symbol": o.symbol, "side": o.side,
                            "size": o.size, "limit": o.limit_price, "stop": o.stop_price}
                           for o in ex.open_orders()],
           "today": act}
    _out(a, doc)
    return 0


def _cmd_alerts(s, a, cfg, src) -> int:
    from desk import alerts as AL
    if a.action == "add":
        if a.from_plan:
            plan = read_json("plan.json")
            if not plan:
                print("no trader/plan.json")
                return 1
            for r in AL.rules_from_plan(plan):
                AL.add_rule(r)
            print(f"added stop/target/funding/liquidation alerts for {plan['symbol']}")
            return 0
        rule = {k: v for k, v in {"type": a.type, "symbol": a.symbol and a.symbol.upper(),
                                  "price": a.price, "side": a.side, "pct": a.pct, "rate": a.rate,
                                  "minutes": a.minutes, "usd": a.usd, "at": a.at, "on": a.on,
                                  "rearm": a.rearm or None, "id": a.id}.items() if v is not None}
        print(json.dumps(AL.add_rule(rule)))
        return 0
    if a.action == "list":
        _out(a, AL.load_rules())
        return 0
    if a.action == "reset":
        print("reset" if AL.reset(a.id or "all") else "no such alert")
        return 0
    if a.action == "quote":
        syms = [x.strip().upper() for x in (a.symbols or ",".join(s.symbols)).split(",")]
        q = {x: src.ticker(x) for x in syms}
        doc = {"created_by": "desk-alerts", "at": time.time(), "quotes": {
            k: {"last": v.get("close"), "mark": v.get("mark_price"), "funding_rate_pct":
                v.get("funding_rate"), "change_24h_pct": v.get("mark_change_24h")} for k, v in q.items()}}
        if a.write:
            write_json("prices.json", doc)
        _out(a, doc)
        return 0
    loops = 0
    while True:
        rules = AL.load_rules()
        syms = sorted({r["symbol"] for r in rules if r.get("symbol")})
        quotes = {}
        for x in syms:
            try:
                quotes[x] = src.ticker(x)
            except Exception:
                pass
        positions = {}
        try:
            ex, _, _ = _paper_state(src, cfg, syms)
            from layers.risk.sizing import liquidation_price
            for k, p in ex.positions().items():
                spec = ex.registry.get(k)
                positions[k] = {"size": p.size, "liq": liquidation_price(p.side, p.entry_price,
                                                                         p.leverage, spec.maintenance_margin)}
        except Exception:
            pass
        rang = AL.check(quotes, positions)
        for r in rang:
            print(f"ALERT {r['id']}: {r['msg']}")
        loops += 1
        if a.action == "check" or (a.max_loops and loops >= a.max_loops):
            return 0 if rang else 1
        time.sleep(a.every)


def _cmd_options(s, a, cfg, src) -> int:
    from desk.options import chain_doc
    rows = src.option_tickers(a.asset.upper())
    doc = chain_doc(a.asset.upper(), rows, a.expiry)
    an = doc["analysis"]
    _out(a, doc, f"{a.asset.upper()} {doc.get('expiry')}: spot {an.get('spot')} ATM {an.get('atm_strike')} "
                 f"IV {an.get('atm_iv')} straddle {an.get('straddle')} (±{an.get('implied_move_pct')}%) "
                 f"PCR {an.get('pcr_oi')} max pain {an.get('max_pain')}\n  call walls "
                 f"{an.get('call_oi_walls')} put walls {an.get('put_oi_walls')}\n  {doc['note']}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit("use: python -m main <command>")
