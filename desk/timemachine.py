"""
Time machine (crypto rebuild of trader-timemachine).

``run``  — 10,000 Monte-Carlo replays of the plan from its entry:
  1. Pool: recent closed candles of the plan's resolution, each stored as ratios
     to the previous close (open, high, low, close) / prev_close, bucketed by
     UTC time-of-day slot (crypto has strong intraday seasonality: Asia / EU / US
     hours, funding times) — the 24/7 equivalent of the equity session-slot pool.
  2. A replay walks forward from the entry slot for up to max_hold_hours, each bar
     drawn from slot ± window, scaled to the running price. The STOP is checked
     before the target in every bar; a bar that opens through the stop fills at
     the open (gap); the estimated liquidation is checked before the stop.
  3. Outcomes: target / stop / liquidation / time (still open at the time stop →
     exit at that bar's close). Net R after taker/maker fees + 18 % GST,
     slippage and funding at the current rate.
``history`` — the same entry/stop/target distances applied at the same UTC time on
  every past day in the data (what this plan would have done historically).
Writes trader/timemachine.json.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

import pandas as pd

from engines.costs import CostModel
from infrastructure.data.market_data import RES_SECONDS
from infrastructure.models import ContractSpec
from infrastructure.workspace import now_iso, write_json
from layers.risk.sizing import liquidation_price


def build_pool(bars: pd.DataFrame, resolution: str) -> dict[int, list[tuple[float, float, float, float]]]:
    step = RES_SECONDS[resolution]
    slots = 86400 // step
    pool: dict[int, list[tuple[float, float, float, float]]] = {i: [] for i in range(slots)}
    prev = None
    for ts, r in bars.iterrows():
        if prev and prev > 0:
            slot = int((ts.timestamp() % 86400) // step)
            pool[slot].append((r.open / prev, r.high / prev, r.low / prev, r.close / prev))
        prev = float(r.close)
    return pool


def simulate(pool: dict[int, list], entry: float, stop: float, target: float, side: str,
             start_slot: int, n_bars: int, liq: float | None = None, replays: int = 10_000,
             seed: int = 19, window: int = 2) -> dict[str, Any]:
    long = side == "buy"
    risk = abs(entry - stop)
    r_target = abs(target - entry) / risk
    slots = len(pool)
    rng = random.Random(seed)
    nonempty = [k for k, v in pool.items() if v]
    if not nonempty:
        raise ValueError("empty pool: not enough history")

    def draw(i: int) -> tuple[float, float, float, float]:
        for _ in range(8):
            j = (start_slot + i + rng.randint(-window, window)) % slots
            if pool[j]:
                return rng.choice(pool[j])
        return rng.choice(pool[rng.choice(nonempty)])

    outcomes: list[str] = []
    exits: list[float] = []
    closes: list[float] = []
    for _ in range(replays):
        p = entry
        out, px = None, None
        for i in range(1, n_bars + 1):
            o, h, lo, c = draw(i)
            op, hi, low, cl = p * o, p * h, p * lo, p * c
            if long:
                if op <= stop:                      # gapped through the stop
                    out, px = ("liquidation", liq) if liq is not None and op <= liq else ("stop", op)
                    break
                if low <= stop:
                    out, px = "stop", stop
                    break
                if hi >= target:
                    out, px = "target", target
                    break
            else:
                if op >= stop:
                    out, px = ("liquidation", liq) if liq is not None and op >= liq else ("stop", op)
                    break
                if hi >= stop:
                    out, px = "stop", stop
                    break
                if low <= target:
                    out, px = "target", target
                    break
            p = cl
        if out is None:
            out, px = "time", p
        outcomes.append(out)
        exits.append(px)
        closes.append(p)
    cnt = {k: outcomes.count(k) for k in ("target", "stop", "liquidation", "time")}
    Rs = [((x - entry) if long else (entry - x)) / risk for x in exits]
    srt = sorted(closes)
    return {"replays": replays, "seed": seed, "window": window, "bars": n_bars,
            "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
            "p_liquidation": round(cnt["liquidation"] / replays, 4),
            "p_time": round(cnt["time"] / replays, 4), "r_at_target": round(r_target, 3),
            "avg_R_gross": round(statistics.mean(Rs), 4),
            "p5_close": round(srt[int(0.05 * (len(srt) - 1))], 6),
            "median_close": round(srt[len(srt) // 2], 6),
            "p95_close": round(srt[int(0.95 * (len(srt) - 1))], 6),
            "_outcomes": outcomes, "_exits": exits}


def net_of_costs(mc: dict[str, Any], spec: ContractSpec, plan: dict[str, Any],
                 slippage_bps: float, funding_rate: float, bars_per_hour: float,
                 cm: CostModel | None = None) -> dict[str, Any]:
    cm = cm or CostModel()
    side, entry, stop = plan["side"], plan["entry"], plan["stop"]
    long = side == "buy"
    n = int(plan.get("ref_contracts") or 1)
    slip = slippage_bps / 10_000
    risk_usd = spec.contract_value * n * abs(entry - stop)
    fill_in = entry * (1 + slip) if long else entry * (1 - slip)
    nets = []
    for out, x in zip(mc["_outcomes"], mc["_exits"]):
        maker = out == "target"
        fill_out = x if maker else (x * (1 - slip) if long else x * (1 + slip))
        gross = spec.pnl(side, n, fill_in, fill_out)
        fees = cm.fee(spec, spec.notional(n, fill_in), False) + \
            cm.fee(spec, spec.notional(n, fill_out), maker)
        hold_h = mc["bars"] / bars_per_hour if out == "time" else mc["bars"] / bars_per_hour / 2
        fund = cm.funding_payment(side, spec.notional(n, fill_in), funding_rate) * \
            (hold_h * 3600 / spec.funding_interval_s) if spec.is_perpetual else 0.0
        if out == "liquidation":
            gross = -spec.notional(n, entry) / plan["leverage"]
        nets.append(gross - fees - fund)
    avg = statistics.mean(nets)
    return {"net_expectancy_usd": round(avg, 6),
            "net_expectancy_r": round(avg / risk_usd, 4) if risk_usd else None,
            "ref_contracts": n, "risk_usd": round(risk_usd, 6)}


def history(bars: pd.DataFrame, plan: dict[str, Any], resolution: str, n_bars: int) -> dict[str, Any]:
    """Same UTC time of day on every past day, same % distances to stop/target."""
    entry, stop, target, side = plan["entry"], plan["stop"], plan["target"], plan["side"]
    long = side == "buy"
    stop_pct, tgt_pct = abs(entry - stop) / entry, abs(target - entry) / entry
    when = pd.Timestamp(plan.get("entry_bar_open_utc") or bars.index[-1])
    tod = when.tz_convert("UTC") - when.tz_convert("UTC").normalize() if when.tzinfo else \
        when - when.normalize()
    res: dict[str, int] = {"target": 0, "stop": 0, "time": 0}
    rs = []
    for day in sorted({ts.normalize() for ts in bars.index})[:-1]:
        t0 = day + tod
        seg = bars[(bars.index > t0)].head(n_bars)
        if t0 not in bars.index or len(seg) < 1:
            continue
        e = float(bars.loc[t0, "close"])
        st = e * (1 - stop_pct) if long else e * (1 + stop_pct)
        tg = e * (1 + tgt_pct) if long else e * (1 - tgt_pct)
        out, x = "time", float(seg["close"].iloc[-1])
        for _, r in seg.iterrows():
            if (long and r.low <= st) or (not long and r.high >= st):
                out, x = "stop", (min(r.open, st) if long else max(r.open, st))
                break
            if (long and r.high >= tg) or (not long and r.low <= tg):
                out, x = "target", tg
                break
        res[out] += 1
        rs.append(((x - e) if long else (e - x)) / (e * stop_pct))
    n = sum(res.values())
    return {"days": n, **res, "avg_R_gross": round(statistics.mean(rs), 4) if rs else None,
            "win_rate": round(res["target"] / n, 3) if n else None}


def verdict(mc: dict[str, Any], net: dict[str, Any]) -> str:
    pt, ps = mc["p_target"], mc["p_stop"] + mc["p_liquidation"]
    hit = pt + ps
    need = 1 / (1 + mc["r_at_target"])
    share = pt / hit if hit else 0
    if hit == 0:
        head = "No replay reached the stop or the target: the time stop decides every trade."
    elif share >= need + 0.08:
        head = "Leans toward the target"
    elif share <= need - 0.08:
        head = "Leans toward the stop"
    else:
        head = "Close to break-even before costs"
    tail = ""
    ne = net.get("net_expectancy_r")
    if ne is not None:
        tail = (f" After fees, GST, slippage and funding: {ne:+.3f} R a trade"
                + (" — the costs eat the edge." if mc["avg_R_gross"] > 0 >= ne else "."))
    return (f"{head}: target first {pt:.1%} vs stop first {ps:.1%} "
            f"(break-even needs {need:.0%} of decided replays at {mc['r_at_target']:g}R).{tail}")


def run(plan: dict[str, Any], spec: ContractSpec, bars: pd.DataFrame, cfg: dict[str, Any],
        replays: int = 10_000, seed: int = 19, window: int = 2, funding_rate: float = 0.0,
        write: bool = False) -> dict[str, Any]:
    resolution = plan.get("resolution") or cfg["resolution"]
    step = RES_SECONDS[resolution]
    n_bars = max(1, int(plan.get("max_hold_hours", cfg["max_hold_hours"]) * 3600 // step))
    pool = build_pool(bars, resolution)
    when = pd.Timestamp(plan.get("entry_bar_open_utc") or bars.index[-1])
    start_slot = int((when.timestamp() % 86400) // step)
    liq = liquidation_price(plan["side"], plan["entry"], plan["leverage"], spec.maintenance_margin)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], plan["side"], start_slot,
                  n_bars, liq, replays, seed, window)
    net = net_of_costs(mc, spec, plan, cfg["slippage_bps"], funding_rate, 3600 / step)
    hist = history(bars, plan, resolution, n_bars)
    doc = {"created_by": "desk-timemachine", "created_at": now_iso(), "symbol": plan["symbol"],
           "plan": {k: plan.get(k) for k in ("side", "entry", "stop", "target", "leverage",
                                              "max_hold_hours")},
           "data": {"bars": len(bars), "from": str(bars.index[0]), "to": str(bars.index[-1]),
                    "resolution": resolution},
           **{k: v for k, v in mc.items() if not k.startswith("_")}, **net, "history": hist,
           "verdict": verdict(mc, net)}
    if write:
        write_json("timemachine.json", doc)
    return doc
