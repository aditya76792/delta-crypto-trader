from __future__ import annotations

import json
import time

import pytest

from engines.live_executor.executor import LiveExecutor
from engines.live_executor.factory import LiveGateError, build_live_executor, check_live_gates
from infrastructure.config.settings import LIVE_CONFIRM_PHRASE, Settings
from infrastructure.exchange.rest import DeltaAPIError
from infrastructure.models import TradePlan


class FakeRest:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.fail_next_place: DeltaAPIError | None = None
        self.open_orders: list[dict] = []
        self.positions: list[dict] = []

    def get_balances(self):
        self.calls.append(("balances", None))
        return [{"asset_symbol": "USD", "balance": "1000", "available_balance": "900"}]

    def set_leverage(self, pid, lev):
        self.calls.append(("leverage", (pid, lev)))

    def place_order(self, body):
        self.calls.append(("place", body))
        if self.fail_next_place:
            e, self.fail_next_place = self.fail_next_place, None
            raise e
        return {"id": 111, "client_order_id": body.get("client_order_id"), "state": "closed",
                "size": body["size"], "unfilled_size": 0, "average_fill_price": "60000.5"}

    def get_open_orders(self, product_id=None):
        self.calls.append(("open_orders", product_id))
        return self.open_orders

    def get_fills(self, product_id=None):
        return []

    def get_positions(self):
        return self.positions

    def cancel_order(self, pid, order_id=None, client_order_id=None):
        self.calls.append(("cancel", client_order_id))

    def cancel_all(self, pid=None):
        self.calls.append(("cancel_all", pid))

    def edit_order(self, body):
        self.calls.append(("edit", body))
        return {"id": body["id"], "state": "open", "size": body.get("size", 3),
                "unfilled_size": body.get("size", 3)}

    def place_bracket(self, body):
        self.calls.append(("bracket", body))


@pytest.fixture
def api_registry(registry):
    registry.source = "api"
    return registry


def _passing_report(d, strategy="ema_trend_scalp", sym="BTCUSD", res="5m"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{strategy}_{sym}_{res}.json").write_text(json.dumps(
        {"passed": True, "created_at": time.time(), "failures": []}))


def live_settings(tmp_path, **kw):
    s = Settings(mode="live", api_key="k", api_secret="s", live_confirm=LIVE_CONFIRM_PHRASE,
                 symbols=["BTCUSD"], data_dir=tmp_path)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def build(tmp_path, api_registry, rest=None, **kw):
    _passing_report(tmp_path / "bt")
    return build_live_executor(live_settings(tmp_path, **kw), rest or FakeRest(), api_registry,
                               True, tmp_path / "bt", False, prompt=lambda _: "LIVE BTCUSD",
                               interactive=True)


def test_direct_construction_blocked(api_registry):
    with pytest.raises(RuntimeError):
        LiveExecutor(FakeRest(), api_registry)


@pytest.mark.parametrize("mut,needle", [
    ({"mode": "paper"}, "TRADING_MODE"),
    ({"live_confirm": ""}, "DELTA_LIVE_CONFIRM"),
    ({"api_key": ""}, "DELTA_API_KEY"),
])
def test_gates_block(tmp_path, api_registry, mut, needle):
    _passing_report(tmp_path / "bt")
    with pytest.raises(LiveGateError, match=needle):
        check_live_gates(live_settings(tmp_path, **mut), api_registry, True, tmp_path / "bt",
                         False, prompt=lambda _: "LIVE BTCUSD", interactive=True)


def test_gate_requires_cli_flag_api_specs_backtest_and_no_kill(tmp_path, registry):
    s = live_settings(tmp_path)
    with pytest.raises(LiveGateError) as e:
        check_live_gates(s, registry, False, tmp_path / "bt", True, interactive=True)
    msg = str(e.value)
    assert "--confirm-live" in msg and "fallback" in msg and "no backtest report" in msg \
        and "kill switch" in msg


def test_gate_typed_confirmation(tmp_path, api_registry):
    _passing_report(tmp_path / "bt")
    with pytest.raises(LiveGateError, match="did not match"):
        check_live_gates(live_settings(tmp_path), api_registry, True, tmp_path / "bt", False,
                         prompt=lambda _: "yes", interactive=True)
    with pytest.raises(LiveGateError, match="NONINTERACTIVE"):
        check_live_gates(live_settings(tmp_path), api_registry, True, tmp_path / "bt", False,
                         prompt=None, interactive=False)


def mk_plan(**kw):
    base = dict(symbol="BTCUSD", side="buy", contracts=3, entry_price=60000.0,
                stop_loss=59400.2, take_profit=61500.3, leverage=5, strategy_id="t")
    base.update(kw)
    return TradePlan(**base)


def test_order_body_has_brackets_rounded_and_leverage_set(tmp_path, api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    o = ex.execute_plan(mk_plan())
    place = [c for c in rest.calls if c[0] == "place"][0][1]
    assert place["product_id"] == 27 and place["size"] == 3 and place["reduce_only"] is False
    assert place["bracket_stop_loss_price"] == "59400.5"       # long stop rounds tighter
    assert place["bracket_take_profit_price"] in ("61500.5", "61500.0")
    assert place["bracket_stop_trigger_method"] == "mark_price"
    assert ("leverage", (27, 5)) in rest.calls
    assert o.state == "closed" and o.filled == 3


def test_duplicate_plan_refused(tmp_path, api_registry):
    ex = build(tmp_path, api_registry)
    p = mk_plan()
    ex.execute_plan(p)
    with pytest.raises(RuntimeError, match="duplicate"):
        ex.execute_plan(p)


def test_ambiguous_network_failure_finds_existing_order_and_does_not_resend(tmp_path,
                                                                           api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    p = mk_plan()
    rest.fail_next_place = DeltaAPIError("network_error", "timeout", retryable=True)
    rest.open_orders = [{"id": 999, "client_order_id": p.client_order_id, "state": "open",
                         "size": 3, "unfilled_size": 3}]
    o = ex.execute_plan(p)
    assert len([c for c in rest.calls if c[0] == "place"]) == 1
    assert o.exchange_id == "999" and o.state == "open"


def test_business_rejection_recorded(tmp_path, api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    rest.fail_next_place = DeltaAPIError("insufficient_margin", "", 400)
    o = ex.execute_plan(mk_plan())
    assert o.state == "rejected" and o.reject_reason == "insufficient_margin"


def test_ws_partial_fill_updates(tmp_path, api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    rest.place_order = lambda body: {"id": 5, "state": "open", "size": 3, "unfilled_size": 3}
    p = mk_plan(order_type="limit_order", limit_price=59990.0)
    ex.execute_plan(p)
    ex.handle_ws({"type": "orders", "client_order_id": p.client_order_id, "id": 5,
                  "state": "open", "size": 3, "unfilled_size": 1, "average_fill_price": "59990"})
    o = ex._orders[p.client_order_id]
    assert o.filled == 2 and o.state == "partially_filled"
    ex.handle_ws({"type": "positions", "product_symbol": "BTCUSD", "size": 2,
                  "entry_price": "59990", "liquidation_price": "48000"})
    assert ex.position("BTCUSD").size == 2
    assert ex.position("BTCUSD").exchange_liq_price == 48000


def test_reconcile_adopts_exchange_and_reprotects(tmp_path, api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    ex.execute_plan(mk_plan())
    rest.positions = [{"product_symbol": "BTCUSD", "size": 3, "entry_price": "60000"}]
    rest.open_orders = []          # no stop on exchange!
    issues = ex.reconcile()
    assert any(i["issue"] == "unprotected_position" for i in issues)
    assert any(c[0] == "bracket" for c in rest.calls)
    rest.positions = []
    issues = ex.reconcile()
    assert any(i["issue"] == "local_position_missing_on_exchange" for i in issues)
    assert not ex.positions()


def test_equity_from_wallet(tmp_path, api_registry):
    ex = build(tmp_path, api_registry)
    assert ex.equity() == 1000 and ex.available_margin() == 900


def test_kill_flatten_closes_with_reduce_only(tmp_path, api_registry):
    rest = FakeRest()
    ex = build(tmp_path, api_registry, rest)
    ex.execute_plan(mk_plan())
    ex.handle_ws({"type": "positions", "product_symbol": "BTCUSD", "size": 3, "entry_price": "60000"})
    assert ex.flatten_all("test") == 1
    close = [c[1] for c in rest.calls if c[0] == "place"][-1]
    assert close["reduce_only"] is True and close["side"] == "sell" and close["size"] == 3
    assert ("cancel_all", None) in rest.calls
