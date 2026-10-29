"""Offline end-to-end desk workflow: scan → entry → exit → timemachine → budget → risk →
broker (explicit yes) → paper fill → alerts. Fixtures mimic the official API shapes."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from infrastructure.data.history import synthetic_candles
from infrastructure.exchange.instruments import FALLBACK_SPECS
from layers.automation.orchestrator import main as cli


def _rows(df: pd.DataFrame) -> list[dict]:
    return [{"time": int(ts.timestamp()), "open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume} for ts, r in df.iterrows()]


def _breakout(bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    base = float(bars["close"].iloc[-61])
    wave = base * (1 + 0.002 * np.sin(np.arange(60) / 3))      # a 60-bar sideways range
    bars.iloc[-60:, bars.columns.get_loc("close")] = wave
    bars.iloc[-60:, bars.columns.get_loc("open")] = np.concatenate([[base], wave[:-1]])
    bars.iloc[-60:, bars.columns.get_loc("high")] = np.maximum(wave, bars["open"].iloc[-60:]) * 1.0004
    bars.iloc[-60:, bars.columns.get_loc("low")] = np.minimum(wave, bars["open"].iloc[-60:]) * 0.9996
    last = bars.iloc[-1]
    hi = bars["high"].iloc[-48:].max()
    ts = bars.index[-1] + pd.Timedelta(minutes=5)
    new_close = hi * 1.004
    row = pd.DataFrame([{"open": last.close, "high": new_close * 1.0005, "low": last.close * 0.9995,
                         "close": new_close, "volume": bars["volume"].iloc[-20:].mean() * 3}],
                       index=pd.DatetimeIndex([ts], name="ts"))
    return pd.concat([bars, row])


def _ticker(sym, px, turnover, funding="0.01"):
    return {"symbol": sym, "contract_type": "perpetual_futures", "close": px, "mark_price": px,
            "spot_price": px, "funding_rate": funding, "turnover_usd": turnover,
            "oi_contracts": 1000, "mark_change_24h": "1.2",
            "quotes": {"best_bid": px - 0.5, "best_ask": px + 0.5}, "timestamp": 0}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    d = tmp_path / "offline"
    d.mkdir()
    btc = _breakout(synthetic_candles(900, start_price=60000, seed=5, annual_vol=0.4))
    eth = synthetic_candles(900, start_price=3000, seed=6, annual_vol=0.5)
    (d / "candles-BTCUSD-5m.json").write_text(json.dumps(_rows(btc)))
    (d / "candles-ETHUSD-5m.json").write_text(json.dumps(_rows(eth)))
    bpx = round(float(btc["close"].iloc[-1]) * 2) / 2
    epx = round(float(eth["close"].iloc[-1]), 1)
    (d / "tickers.json").write_text(json.dumps([
        _ticker("BTCUSD", bpx, 5e8), _ticker("ETHUSD", epx, 2e8),
        _ticker("XYZUSD", 1.0, 1e3)]))
    (d / "products.json").write_text(json.dumps(list(FALLBACK_SPECS.values())))
    (d / "orderbook-BTCUSD.json").write_text(json.dumps({
        "buy": [{"price": bpx - 0.5 - i * 0.5, "size": 5000} for i in range(10)],
        "sell": [{"price": bpx + 0.5 + i * 0.5, "size": 5000} for i in range(10)]}))
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADER_LOG_DIR", str(tmp_path / "logs"))
    return str(d)


def run(capsys, *args):
    rc = cli(list(args))
    return rc, capsys.readouterr().out


def test_full_desk_flow(offline, capsys):
    from infrastructure.workspace import read_json
    rc, out = run(capsys, "scan", "--offline", offline)
    assert rc == 0
    wl = read_json("watchlist.json")
    assert "XYZUSD" in wl["skipped"]                               # illiquid skipped
    assert any(h["symbol"] == "BTCUSD" and h["setup"] == "range_breakout" and h["side"] == "buy"
               for h in wl["hits"])

    rc, out = run(capsys, "entry", "check", "BTCUSD", "--offline", offline, "--write")
    assert "TRIGGERED" in out
    assert read_json("entry.json")["status"] == "triggered"

    rc, out = run(capsys, "exit", "plan", "--offline", offline, "--stop", "atr:1.5", "--write")
    assert rc == 0, out
    plan = read_json("plan.json")
    assert plan["stop"] < plan["entry"] < plan["target"]
    assert (plan["stop"] / 0.5).is_integer() and (plan["target"] / 0.5).is_integer()
    assert plan["liquidation_price_est"] < plan["stop"]

    rc, out = run(capsys, "timemachine", "--offline", offline, "--replays", "2000", "--write")
    tm = read_json("timemachine.json")
    assert abs(tm["p_target"] + tm["p_stop"] + tm["p_liquidation"] + tm["p_time"] - 1) < 1e-6
    assert "net_expectancy_r" in tm and tm["history"]["days"] >= 1

    rc, out = run(capsys, "budget", "--offline", offline, "--write")
    assert rc == 0, out
    order = read_json("orders.json")["orders"][0]
    assert isinstance(order["size"], int) and order["size"] >= 1
    assert order["costs"]["loss_at_stop_with_fees"] <= 10.0 + 1e-9    # default max loss
    assert order["delta_body"]["bracket_stop_loss_price"] == str(order["stop_loss"])

    # not checked yet → cannot approve
    rc, out = run(capsys, "broker", "approve", "--index", "1", "--reply", "yes", "--offline", offline)
    assert rc == 2 and "NOT CHECKED" in out

    rc, out = run(capsys, "risk", "--offline", offline)
    assert rc == 0, out
    assert "[PASS]" in out

    rc, out = run(capsys, "broker", "show", "--offline", offline)
    assert "READY" in out
    for bad in ("ok", "sure go", "yes to all", "yes but smaller", "yes ETHUSD", "yes 999"):
        rc, out = run(capsys, "broker", "approve", "--index", "1", "--reply", bad, "--offline", offline)
        assert rc == 2, bad
    rc, out = run(capsys, "broker", "place", "--index", "1", "--offline", offline)
    assert rc == 2 and "no explicit yes" in out
    rc, out = run(capsys, "broker", "approve", "--index", "1", "--reply",
                  f"yes buy {order['size']} BTCUSD", "--offline", offline)
    assert rc == 0, out
    rc, out = run(capsys, "broker", "place", "--index", "1", "--offline", offline)
    assert rc == 0, out
    placed = json.loads(out)
    assert placed["mode"] == "paper" and placed["filled"] == order["size"]

    rc, out = run(capsys, "paper-account", "status", "--offline", offline)
    acct = json.loads(out)
    assert acct["positions"]["BTCUSD"]["size"] == order["size"]
    assert len(acct["open_orders"]) == 2                            # stop + target brackets
    # placing the same ticket twice is refused
    rc, out = run(capsys, "broker", "place", "--index", "1", "--offline", offline)
    assert rc == 2

    rc, out = run(capsys, "alerts", "add", "--from-plan", "--offline", offline)
    rc, out = run(capsys, "alerts", "add", "--type", "above", "--symbol", "BTCUSD", "--price", "1",
                  "--offline", offline)
    rc, out = run(capsys, "alerts", "check", "--offline", offline)
    assert rc == 0 and "ALERT" in out
    rc, out = run(capsys, "alerts", "check", "--offline", offline)
    assert "btcusd-above" not in out                              # rings once


def test_costs_and_clock(offline, capsys):
    rc, out = run(capsys, "costs", "--symbol", "BTCUSD", "--entry", "60000", "--exit", "60600",
                  "--contracts", "10", "--hold-hours", "8", "--funding-rate", "0.0001", "--offline", offline)
    c = json.loads(out)
    assert c["fee_in"] == pytest.approx(600 * 0.0005 * 1.18)
    assert c["funding"] == pytest.approx(600 * 0.0001)
    assert c["gross_pnl"] == pytest.approx(6.0)
    rc, out = run(capsys, "clock", "--offline", offline)
    assert "24/7" in out


def test_options_chain(tmp_path, capsys, monkeypatch):
    d = tmp_path / "off"
    d.mkdir()
    rows = []
    for k in (58000, 60000, 62000):
        for kind, oi in (("C", 100 if k > 60000 else 50), ("P", 120 if k < 60000 else 40)):
            rows.append({"symbol": f"{kind}-BTC-{k}-311226", "mark_price": "800" if k == 60000 else "300",
                         "spot_price": "60100", "oi_contracts": str(oi),
                         "quotes": {"best_bid": "790", "best_ask": "810", "mark_iv": "0.5"},
                         "greeks": {"delta": "0.5"}})
    (d / "options-BTC.json").write_text(json.dumps(rows))
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    rc, out = run(capsys, "options", "--asset", "BTC", "--offline", str(d), "--json")
    doc = json.loads(out)
    an = doc["analysis"]
    assert an["atm_strike"] == 60000 and an["straddle"] == 1600
    assert an["pcr_oi"] == pytest.approx((120 + 40 + 40) / (50 + 50 + 100))
    assert an["max_pain"] in (58000, 60000, 62000)


def test_exit_refuses_stop_beyond_liquidation(offline, capsys):
    rc, out = run(capsys, "exit", "plan", "--offline", offline, "--symbol", "BTCUSD", "--side", "buy",
                  "--entry", "60000", "--stop", "pct:15", "--leverage", "10")
    assert rc == 2 and "liquidation" in out


def test_parse_yes_rules():
    from desk.broker import parse_yes
    o = {"symbol": "BTCUSD", "side": "buy", "size": 7, "limit_price": None}
    assert parse_yes("yes", o)[0]
    assert parse_yes("Yes, buy 7 BTCUSD", o)[0]
    assert not parse_yes("yes 8", o)[0]
    assert not parse_yes("yes?", o)[0]
    assert not parse_yes("okay", o)[0]
    assert not parse_yes("yes ETHUSD", o, ("BTCUSD", "ETHUSD"))[0]
    assert not parse_yes("yes all", o)[0]
