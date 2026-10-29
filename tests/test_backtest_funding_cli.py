"""Regression: a real funding Series from the exchange must not be truth-tested."""

from __future__ import annotations

import pandas as pd

from infrastructure.data.history import synthetic_candles
from layers.automation import orchestrator as O


def test_backtest_uses_funding_series(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / "logs"))
    bars = synthetic_candles(900)
    monkeypatch.setattr(O, "_bars", lambda s, sym, days, synth, online=True: (bars, "exchange"))
    seen = {}

    def fake_funding(s, sym, b):
        seen["called"] = True
        return pd.Series([0.0001] * len(b), index=b.index)
    monkeypatch.setattr(O, "_funding_series", fake_funding)
    from infrastructure.exchange.instruments import InstrumentRegistry
    monkeypatch.setattr(O, "_registry", lambda s, online=True: InstrumentRegistry.fallback())
    assert O.main(["--symbols", "BTCUSD", "backtest", "--days", "3"]) == 0
    assert seen.get("called")
