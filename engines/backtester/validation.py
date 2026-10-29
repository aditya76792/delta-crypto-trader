"""
Pre-deployment validation gate.

A strategy/symbol/resolution combination must have a PASSING backtest report no
older than ``max_age_days`` before ``live`` mode will start (paper mode only warns).
Criteria are deliberately modest minimums — passing is necessary, not sufficient.
Reports are written to data/backtests/<strategy>_<symbol>_<res>.json.

Also provides an out-of-sample split: params are fixed, the series is cut into
in-sample / out-of-sample segments and BOTH must pass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from engines.backtester.engine import Backtester


@dataclass
class GateCriteria:
    min_trades: int = 30
    min_profit_factor: float = 1.1
    max_drawdown_pct: float = 25.0
    min_expectancy_r: float = 0.0
    max_liquidations: int = 0


def evaluate(metrics: dict[str, Any], crit: GateCriteria) -> tuple[bool, list[str]]:
    fails = []
    if metrics["trades"] < crit.min_trades:
        fails.append(f"trades {metrics['trades']} < {crit.min_trades}")
    pf = metrics["profit_factor"]
    pf_v = float("inf") if pf == "inf" else float(pf)
    if pf_v < crit.min_profit_factor:
        fails.append(f"profit_factor {pf} < {crit.min_profit_factor}")
    if abs(metrics["max_drawdown_pct"]) > crit.max_drawdown_pct:
        fails.append(f"max_drawdown {metrics['max_drawdown_pct']}% > {crit.max_drawdown_pct}%")
    if metrics["expectancy_r"] <= crit.min_expectancy_r:
        fails.append(f"expectancy_r {metrics['expectancy_r']} <= {crit.min_expectancy_r}")
    if metrics["liquidations"] > crit.max_liquidations:
        fails.append(f"liquidations {metrics['liquidations']}")
    return not fails, fails


def run_validation(bt: Backtester, bars: pd.DataFrame, symbol: str, resolution: str,
                   out_dir: Path, crit: GateCriteria | None = None, oos_frac: float = 0.3,
                   data_source: str = "exchange") -> dict[str, Any]:
    crit = crit or GateCriteria()
    cut = int(len(bars) * (1 - oos_frac))
    full = bt.run(bars, symbol).metrics()
    ins = bt.run(bars.iloc[:cut], symbol).metrics()
    oos = bt.run(bars.iloc[max(0, cut - bt.strategy.required_warmup_bars):], symbol).metrics()
    ok_full, f_full = evaluate(full, crit)
    oos_crit = GateCriteria(max(5, crit.min_trades // 4), crit.min_profit_factor,
                            crit.max_drawdown_pct, crit.min_expectancy_r, crit.max_liquidations)
    ok_oos, f_oos = evaluate(oos, oos_crit)
    passed = ok_full and ok_oos and data_source == "exchange"
    report = {
        "strategy": bt.strategy.strategy_id, "symbol": symbol, "resolution": resolution,
        "params": bt.strategy.params, "leverage": bt.leverage, "bars": len(bars),
        "from": str(bars.index[0]), "to": str(bars.index[-1]), "data_source": data_source,
        "full": full, "in_sample": ins, "out_of_sample": oos,
        "passed": passed, "failures": f_full + [f"OOS: {x}" for x in f_oos] + (
            [] if data_source == "exchange" else ["data is not real exchange history"]),
        "created_at": time.time(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{bt.strategy.strategy_id}_{symbol}_{resolution}.json").write_text(
        json.dumps(report, indent=2, default=str))
    return report


def gate_status(out_dir: Path, strategy: str, symbol: str, resolution: str,
                max_age_days: float = 14) -> tuple[bool, str]:
    p = out_dir / f"{strategy}_{symbol}_{resolution}.json"
    if not p.exists():
        return False, f"no backtest report at {p}"
    r = json.loads(p.read_text())
    age_d = (time.time() - r.get("created_at", 0)) / 86400
    if age_d > max_age_days:
        return False, f"backtest report is {age_d:.1f} days old (> {max_age_days})"
    if not r.get("passed"):
        return False, "backtest gate FAILED: " + "; ".join(r.get("failures", []))
    return True, "ok"
