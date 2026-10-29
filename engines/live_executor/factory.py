"""
The ONLY way to construct a LiveExecutor. Every real-money gate lives here.

Gates (all must pass):
  1. TRADING_MODE=live (settings.mode)
  2. DELTA_LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_MONEY in the environment
  3. ``--confirm-live`` CLI flag AND an interactive typed confirmation
     (the symbol list + the word LIVE) unless running non-interactively with
     DELTA_LIVE_NONINTERACTIVE=1 (for supervised server deployment)
  4. API credentials present; authenticated wallet call succeeds
  5. Instrument specs loaded from the API (not cache/fallback)
  6. A passing, recent backtest report for every strategy/symbol (validation gate)
  7. Kill switch not engaged
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from engines.backtester.validation import gate_status
from infrastructure.config.settings import Settings
from infrastructure.exchange.instruments import InstrumentRegistry
from infrastructure.exchange.rest import DeltaRestClient

_GATE_TOKEN = object()


class LiveGateError(RuntimeError):
    pass


def check_live_gates(settings: Settings, registry: InstrumentRegistry, cli_confirm: bool,
                     backtest_dir: Path, kill_switch_active: bool,
                     prompt: Callable[[str], str] | None = input,
                     interactive: bool | None = None) -> None:
    errors: list[str] = []
    if settings.mode != "live":
        errors.append("TRADING_MODE is not 'live'")
    if not settings.live_confirmed:
        errors.append("DELTA_LIVE_CONFIRM must equal I_UNDERSTAND_THIS_USES_REAL_MONEY")
    if not cli_confirm:
        errors.append("--confirm-live flag missing")
    if not settings.has_credentials:
        errors.append("DELTA_API_KEY / DELTA_API_SECRET not set")
    if registry.source != "api":
        errors.append(f"instrument specs came from {registry.source!r}, not the live API")
    for sym in settings.symbols:
        ok, why = gate_status(backtest_dir, settings.strategy, sym, settings.resolution)
        if not ok:
            errors.append(f"{sym}: {why}")
    if kill_switch_active:
        errors.append("kill switch is engaged (python -m main kill --release)")
    if errors:
        raise LiveGateError("LIVE TRADING BLOCKED:\n  - " + "\n  - ".join(errors))

    interactive = sys.stdin.isatty() if interactive is None else interactive
    if interactive and prompt is not None:
        expected = f"LIVE {','.join(settings.symbols)}"
        typed = prompt(f"\nREAL MONEY on Delta Exchange India ({settings.env}).\n"
                       f"Type exactly '{expected}' to continue: ").strip()
        if typed != expected:
            raise LiveGateError("confirmation text did not match; aborting")
    elif os.getenv("DELTA_LIVE_NONINTERACTIVE") != "1":
        raise LiveGateError("non-interactive live start requires DELTA_LIVE_NONINTERACTIVE=1")


def build_live_executor(settings: Settings, rest: DeltaRestClient, registry: InstrumentRegistry,
                        cli_confirm: bool, backtest_dir: Path, kill_switch_active: bool,
                        prompt: Callable[[str], str] | None = input,
                        interactive: bool | None = None):
    from engines.live_executor.executor import LiveExecutor
    check_live_gates(settings, registry, cli_confirm, backtest_dir, kill_switch_active,
                     prompt, interactive)
    rest.get_balances()   # proves auth + IP whitelist + permissions before trading
    return LiveExecutor(rest, registry, use_brackets=settings.use_bracket_orders,
                        _gate_token=_GATE_TOKEN)
