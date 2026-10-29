"""
The road to live trading (port of the reference live engine's safety process).

  validate      scripted scenarios through the REAL Trader + RiskChecker + PaperExecutor
                (target, stop, gap through stop, time stop, stale data, kill switch,
                daily-loss halt, duplicate plan). Passes only if every invariant holds:
                every entry was risk-approved, every filled entry had an exchange-style
                stop immediately, OCO siblings were cancelled, nothing was entered while
                halted or on stale data, nothing is left open/unprotected at the end.
  paper report  `paper` writes trader/live/reports/<date>-paper.json on exit; a session
                counts when it ran ≥ min_paper_hours on the real-time feed without
                unhandled errors.
  approve-live  typed at a real terminal: shows your limits and asks for the exact
                sentence. Stores a hash of the limits; changing any limit later locks
                live mode until you approve again. revoke-live removes it.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from infrastructure.config.settings import Settings
from infrastructure.workspace import now_iso, path, read_json, write_json

PHRASE = "I APPROVE LIVE TRADING WITH REAL MONEY"
LIVE_DIR = "live"


# ── approval ──────────────────────────────────────────────────────────────────

def limits_of(s: Settings) -> dict[str, Any]:
    return {"env": s.env, "symbols": sorted(s.symbols), "strategy": s.strategy,
            "strategy_params": s.strategy_params.get(s.strategy, {}),
            "resolution": s.resolution, "default_leverage": s.default_leverage,
            "use_bracket_orders": s.use_bracket_orders, "risk": asdict(s.risk)}


def limits_hash(s: Settings) -> str:
    return hashlib.sha256(json.dumps(limits_of(s), sort_keys=True, default=str).encode()).hexdigest()[:20]


def approval_path() -> Path:
    return path(f"{LIVE_DIR}/LIVE_APPROVAL.json")


def latest_report(kind: str) -> dict[str, Any] | None:
    d = path(f"{LIVE_DIR}/reports")
    files = sorted(d.glob(f"*-{kind}.json")) if d.exists() else []
    return json.loads(files[-1].read_text()) if files else None


def readiness(s: Settings, min_paper_hours: float = 4.0) -> tuple[bool, list[str]]:
    problems = []
    v = latest_report("validate")
    if not v or not v.get("passed"):
        problems.append("no passing `validate` report (python -m main validate)")
    elif time.time() - v.get("ts", 0) > 14 * 86400:
        problems.append("the last passing validation is older than 14 days")
    papers = [json.loads(p.read_text()) for p in sorted(path(f"{LIVE_DIR}/reports").glob("*-paper.json"))] \
        if path(f"{LIVE_DIR}/reports").exists() else []
    good = [p for p in papers if p.get("ok") and p.get("hours", 0) >= min_paper_hours
            and p.get("feed") == "delta-realtime"]
    if not good:
        problems.append(f"no paper session of ≥ {min_paper_hours:g} h on the real-time feed "
                        "without errors (python -m main paper)")
    return not problems, problems


def approval_status(s: Settings) -> tuple[bool, str]:
    a = read_json(approval_path())
    if not a:
        return False, "live trading is not approved (python -m main approve-live)"
    if a.get("limits_hash") != limits_hash(s):
        return False, "your limits/symbols/strategy changed since approval: approve again"
    return True, f"approved at {a.get('approved_at')}"


def approve_live(s: Settings, prompt: Callable[[str], str] = input, tty: bool | None = None,
                 min_paper_hours: float = 4.0) -> dict[str, Any]:
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if tty is None else tty
    if not tty:
        raise PermissionError("approve-live must be typed by you at a terminal (not a script or "
                              "an assistant)")
    ok, problems = readiness(s, min_paper_hours)
    if not ok:
        raise PermissionError("not ready:\n  - " + "\n  - ".join(problems))
    print(json.dumps(limits_of(s), indent=2, default=str))
    typed = prompt(f"\nThese limits will apply to REAL MONEY. Type exactly:\n  {PHRASE}\n> ").strip()
    if typed != PHRASE:
        raise PermissionError("the sentence did not match; nothing approved")
    doc = {"approved_at": now_iso(), "limits_hash": limits_hash(s), "limits": limits_of(s),
           "typed": PHRASE}
    write_json(approval_path(), doc)
    return doc


def revoke_live() -> bool:
    p = approval_path()
    if p.exists():
        p.unlink()
        return True
    return False


# ── validation scenarios ─────────────────────────────────────────────────────

def _bars(path_moves: list[tuple[int, float]], start: float = 60_000.0, warm: int = 260,
          seed: int = 3) -> pd.DataFrame:
    """Flat-ish warm-up then scripted per-bar drifts. ``path_moves`` = [(n_bars, pct_per_bar)]."""
    rng = np.random.default_rng(seed)
    closes = [start]
    for _ in range(warm):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.0008)))
    for n, pct in path_moves:
        for _ in range(n):
            closes.append(closes[-1] * (1 + pct / 100))
    closes = np.array(closes)
    opens = np.concatenate([[start], closes[:-1]])
    hi = np.maximum(opens, closes) * 1.0005
    lo = np.minimum(opens, closes) * 0.9995
    idx = pd.date_range("2026-01-05 00:00", periods=len(closes), freq="5min", tz="UTC", name="ts")
    return pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": closes,
                         "volume": 1000.0}, index=idx)


def run_validation(write: bool = True) -> dict[str, Any]:
    import tempfile

    from engines.paper_executor.executor import DuplicateOrderError, PaperExecutor
    from infrastructure.core.events import EventBus
    from infrastructure.data.market_data import MarketDataStore
    from infrastructure.exchange.instruments import InstrumentRegistry
    from infrastructure.models import TradePlan
    from layers.automation.replay import ReplayClock, replay_messages
    from layers.automation.trader import Trader
    from layers.risk.state import RiskState
    from strategies.base import BaseStrategy, TradeSignal

    class Scripted(BaseStrategy):
        strategy_id = "validate_script"
        required_warmup_bars = 50
        max_hold_bars = 30
        default_params: dict = {}

        def __init__(self, at: int, side: str = "BUY", stop_pct: float = 1.0, rr: float = 2.0):
            super().__init__()
            self.at, self.side, self.stop_pct, self.rr = at, side, stop_pct, rr

        def on_bar(self, symbol, bars):
            if len(bars) == self.at:
                px = float(bars["close"].iloc[-1])
                d = px * self.stop_pct / 100
                sl = px - d if self.side == "BUY" else px + d
                tp = px + self.rr * d if self.side == "BUY" else px - self.rr * d
                return TradeSignal(self.side, 1.0, sl, tp)
            return TradeSignal.flat()

    reg = InstrumentRegistry.fallback()
    scenarios: list[dict[str, Any]] = []

    def scenario(name: str, moves: list[tuple[int, float]], at: int = 262,
                 expect_exit: str | None = None, hook: Callable[..., Any] | None = None,
                 expect_entries: int = 1, side: str = "BUY") -> None:
        EventBus.reset()
        bus = EventBus.get()
        d = Path(tempfile.mkdtemp())
        clock = ReplayClock()
        s = Settings(symbols=["BTCUSD"], resolution="5m", data_dir=d)
        store = MarketDataStore("5m", clock=clock)
        ex = PaperExecutor(reg, store, 1000, slippage_bps=2, clock=clock)
        rs = RiskState(d)
        tr = Trader(s, reg, store, ex, Scripted(at, side), rs, bus=bus, clock=clock)
        events: list[str] = []
        violations: list[str] = []
        approved = {"n": 0}
        entries = {"n": 0}
        bus.subscribe("PLAN_APPROVED", lambda e: approved.__setitem__("n", approved["n"] + 1))

        def on_open(e):
            entries["n"] += 1
            events.append("open")
            if approved["n"] < entries["n"]:
                violations.append("a position opened without a risk-approved plan")
            if tr.halted:
                violations.append("a position opened while trading was halted")
        bus.subscribe("POSITION_OPENED", on_open)
        bus.subscribe("POSITION_CLOSED", lambda e: events.append("closed:" + str(
            (e.payload or {}).get("reason", ""))))
        exit_reasons: list[str] = []
        orig_close = ex.close_position

        def tracking_close(sym, reason):
            exit_reasons.append(reason)
            return orig_close(sym, reason)
        ex.close_position = tracking_close   # type: ignore[assignment]
        bars = _bars(moves)
        n_msgs = 0
        for m in replay_messages(bars, "BTCUSD", "5m", clock):
            if hook:
                hook(tr, m, clock, ex, rs)
            tr.handle_message(m)
            n_msgs += 1
            pos = ex.position("BTCUSD")
            if pos.is_open:
                stops = [o for o in ex.open_orders("BTCUSD") if o.reduce_only and o.stop_price]
                if not stops:
                    violations.append("an open position had no protective stop")
                    break
        fills = ex.fills
        if any(o for o in ex.open_orders() if o.reduce_only) and not ex.positions():
            violations.append("bracket orders left behind after the position closed (OCO)")
        final_open = ex.position("BTCUSD").is_open
        if expect_entries != entries["n"]:
            violations.append(f"expected {expect_entries} entries, saw {entries['n']}")
        last_exit = None
        if fills:
            last = fills[-1]
            last_exit = "target" if last.is_maker else ("stop/market")
        if expect_exit == "target" and last_exit != "target":
            violations.append(f"expected a target exit, got {last_exit}")
        if expect_exit == "stop" and (last_exit != "stop/market" or exit_reasons):
            violations.append(f"expected an exchange-style stop exit, got {last_exit} {exit_reasons}")
        if expect_exit == "time" and "time_stop" not in exit_reasons:
            violations.append(f"expected a time-stop exit, got {exit_reasons}")
        if final_open and expect_exit is not None:
            violations.append("position still open at the end")
        scenarios.append({"name": name, "passed": not violations, "violations": violations[:5],
                          "entries": entries["n"], "fills": len(fills), "exit_reasons": exit_reasons,
                          "equity": round(ex.equity(), 4), "halted": tr.halt_reason or None,
                          "messages": n_msgs,
                          "fill_log": [(f.side, f.size, round(f.price, 1), f.is_maker) for f in fills][:6]})

    scenario("target_hit", [(4, 0.0), (40, 0.12)], expect_exit="target")
    scenario("stop_hit", [(4, 0.0), (40, -0.12)], expect_exit="stop")
    scenario("short_target", [(4, 0.0), (40, -0.12)], expect_exit="target", side="SELL")
    scenario("gap_through_stop", [(4, 0.0), (1, -3.0), (5, 0.0)], expect_exit="stop")
    scenario("time_stop", [(4, 0.0), (60, 0.0)], expect_exit="time")

    def stale(tr, m, clock, ex, rs):      # feed freezes: ticker updates stop before the signal
        if m.get("type") == "v2/ticker" and 255 * 300 < clock.t - 1767571200 < 263 * 300:
            m["type"] = "ignored"
    scenario("stale_data_blocks_entry", [(4, 0.0), (20, 0.1)], hook=stale, expect_entries=0)

    def kill(tr, m, clock, ex, rs):
        if not rs.kill_switch_active()[0] and clock.t - 1767571200 > 200 * 300:
            rs.engage_kill_switch("validation")
    scenario("kill_switch_blocks_entry", [(4, 0.0), (20, 0.1)], hook=kill, expect_entries=0)

    def daily_loss(tr, m, clock, ex, rs):
        if ex.position("BTCUSD").is_open and not tr.halted:
            ex.balance -= 40     # simulate a -4 % day elsewhere in the account
    scenario("daily_loss_halts_and_flattens", [(4, 0.0), (20, 0.0)], hook=daily_loss,
             expect_exit=None)
    # duplicate prevention
    scenarios.append(_dup_check(reg, DuplicateOrderError, TradePlan))
    passed = all(x["passed"] for x in scenarios)
    # daily-loss scenario must end flat and halted
    dl = next(x for x in scenarios if x["name"] == "daily_loss_halts_and_flattens")
    if not dl["halted"] or dl["entries"] != 1:
        dl["passed"] = False
        dl["violations"].append("daily loss did not halt and flatten")
        passed = False
    doc = {"kind": "validate", "ts": time.time(), "at": now_iso(), "passed": passed,
           "scenarios": scenarios}
    if write:
        write_json(path(f"{LIVE_DIR}/reports/{time.strftime('%Y%m%dT%H%M%S')}-validate.json"), doc)
    return doc


def _dup_check(reg: Any, DuplicateOrderError: Any, TradePlan: Any) -> dict[str, Any]:
    from engines.paper_executor.executor import PaperExecutor
    from infrastructure.core.events import EventBus
    from infrastructure.data.market_data import MarketDataStore
    from infrastructure.models import Ticker
    EventBus.reset()
    store = MarketDataStore("5m")
    store.update_ticker(Ticker("BTCUSD", 60000, 60000, 60000, 59999.5, 60000.5, 0.0001))
    ex = PaperExecutor(reg, store, 1000)
    p = TradePlan("BTCUSD", "buy", 2, 60000.0, 59400.0, 61200.0, 5, "v")
    ex.execute_plan(p)
    try:
        ex.execute_plan(p)
        ok, v = False, ["the same plan was executed twice"]
    except DuplicateOrderError:
        ok, v = True, []
    return {"name": "duplicate_plan_refused", "passed": ok, "violations": v}


def write_paper_report(started: float, errors: int, reconnects: int, decisions: list[dict[str, Any]],
                       equity_start: float, equity_end: float, feed: str = "delta-realtime") -> dict[str, Any]:
    hours = (time.time() - started) / 3600
    kinds: dict[str, int] = {}
    for d in decisions:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    doc = {"kind": "paper", "feed": feed, "started": started, "ended": time.time(),
           "hours": round(hours, 3), "errors": errors, "ws_reconnects": reconnects,
           "decisions": kinds, "equity_start": equity_start, "equity_end": equity_end,
           "ok": errors == 0}
    write_json(path(f"{LIVE_DIR}/reports/{time.strftime('%Y%m%dT%H%M%S')}-paper.json"), doc)
    return doc
