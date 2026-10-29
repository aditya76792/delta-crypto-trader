"""End-to-end: WS-shaped feed → Trader → RiskChecker → PaperExecutor."""

from __future__ import annotations

import pytest

from engines.paper_executor.executor import PaperExecutor
from infrastructure.config.settings import Settings
from infrastructure.data.history import synthetic_candles
from infrastructure.data.market_data import MarketDataStore
from layers.automation.orchestrator import main as cli
from layers.automation.replay import ReplayClock, replay_messages
from layers.automation.trader import Trader
from layers.risk.state import RiskState
from strategies import build_strategy
from strategies.base import TradeSignal


def stack(tmp_path, registry, strategy="ema_trend_scalp"):
    clock = ReplayClock()
    s = Settings(symbols=["BTCUSD"], resolution="5m", strategy=strategy, data_dir=tmp_path)
    store = MarketDataStore("5m", clock=clock)
    ex = PaperExecutor(registry, store, 1000, clock=clock)
    rs = RiskState(tmp_path)
    tr = Trader(s, registry, store, ex, build_strategy(strategy), rs, clock=clock)
    return clock, store, ex, rs, tr


def test_replay_trades_end_to_end(tmp_path, registry):
    clock, store, ex, rs, tr = stack(tmp_path, registry)
    for m in replay_messages(synthetic_candles(1200), "BTCUSD", "5m", clock):
        tr.handle_message(m)
    kinds = {d["kind"] for d in tr.decisions}
    assert "order" in kinds
    assert len(ex.fills) >= 2
    assert ex.equity() != 1000
    # every fill was on-tick and charged
    for f in ex.fills:
        assert f.fee > 0


def test_stale_data_blocks_entries(tmp_path, registry):
    clock, store, ex, rs, tr = stack(tmp_path, registry)
    bars = synthetic_candles(400)
    for m in replay_messages(bars, "BTCUSD", "5m", clock):
        tr.handle_message(m)
    clock.t += 120     # feed goes silent
    sig = TradeSignal("BUY", 1, store.tickers["BTCUSD"].last * 0.99,
                      store.tickers["BTCUSD"].last * 1.03)
    n = len(ex.fills)
    tr.try_enter("BTCUSD", sig, store.bars("BTCUSD"))
    assert len(ex.fills) == n
    assert "CR-12" in tr.decisions[-1]["detail"]


def test_kill_switch_file_flattens_and_halts(tmp_path, registry):
    clock, store, ex, rs, tr = stack(tmp_path, registry)
    msgs = list(replay_messages(synthetic_candles(300), "BTCUSD", "5m", clock))
    for m in msgs:
        tr.handle_message(m)
    px = store.tickers["BTCUSD"].last
    tr.try_enter("BTCUSD", TradeSignal("BUY", 1, px * 0.99, px * 1.03), store.bars("BTCUSD"))
    assert ex.position("BTCUSD").is_open
    rs.engage_kill_switch("test")
    tr.watchdog()
    assert tr.halted and not ex.position("BTCUSD").is_open


def test_daily_loss_limit_triggers_kill(tmp_path, registry):
    clock, store, ex, rs, tr = stack(tmp_path, registry)
    for m in replay_messages(synthetic_candles(300), "BTCUSD", "5m", clock):
        tr.handle_message(m)
    rs.roll_day_if_needed(ex.equity())
    ex.balance -= 50     # -5 % (limit is -3 %)
    tr.watchdog()
    assert tr.halted and rs.kill_switch_active()[0]


def test_cli_backtest_and_replay_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / "logs"))
    assert cli(["--symbols", "BTCUSD", "backtest", "--synthetic", "--offline", "--days", "3"]) == 0
    assert cli(["--symbols", "BTCUSD", "replay", "--synthetic", "--offline", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert '"equity"' in out


def test_cli_live_blocked_without_gates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DELTA_REST_URL", "http://127.0.0.1:9")   # unreachable on purpose
    rc = cli(["live"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "LIVE TRADING BLOCKED" in out


def test_cli_kill_and_release(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / "logs"))
    assert cli(["kill", "--reason", "x"]) == 0
    assert (tmp_path / "paper" / "KILL_SWITCH").exists()
    assert cli(["kill", "--release"]) == 0
    assert not (tmp_path / "paper" / "KILL_SWITCH").exists()


@pytest.mark.parametrize("secret", ["DELTA_API_SECRET"])
def test_secrets_never_logged(tmp_path, secret):
    import json
    import logging

    from infrastructure.core.logger import configure_logging, get_logger
    configure_logging(tmp_path, also_stdout=False)
    get_logger("t").info("x", api_secret="supersecret", payload={"signature": "abc"})
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "trader.jsonl").read_text()
    assert "supersecret" not in text and "abc" not in json.loads(text.splitlines()[-1])["payload"].values()
