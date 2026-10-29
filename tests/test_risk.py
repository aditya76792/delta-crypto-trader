from __future__ import annotations

import time

import pytest

from infrastructure.config.settings import RiskSettings
from infrastructure.models import TradePlan
from layers.risk.checker import CheckContext, RiskChecker
from layers.risk.state import RiskState


def plan(**kw) -> TradePlan:
    base = dict(symbol="BTCUSD", side="buy", contracts=5, entry_price=60000.0,
                stop_loss=59400.0, take_profit=61500.0, leverage=5, strategy_id="t")
    base.update(kw)
    return TradePlan(**base)


def ctx(**kw) -> CheckContext:
    base = dict(equity=1000, day_start_equity=1000, last_price=60000, mark_price=60000,
                funding_rate=0.0001, spread_pct=0.0001, data_age_s=1.0, atr=150)
    base.update(kw)
    return CheckContext(**base)


@pytest.fixture
def checker():
    return RiskChecker(RiskSettings())


def test_clean_plan_approved(checker, btc):
    r = checker.check(plan(), btc, ctx())
    assert r.approved, r.summary()


@pytest.mark.parametrize("p_kw,c_kw,rule", [
    ({"stop_loss": 60100.0}, {}, "CR-01"),                     # stop on wrong side
    ({"contracts": 0}, {}, "CR-02"),
    ({"contracts": 50}, {}, "CR-03"),                          # too much risk
    ({}, {"equity": 960, "day_start_equity": 1000}, "CR-04"),  # -4 % day
    ({}, {"open_position_count": 3}, "CR-05"),
    ({}, {"symbol_exposure": 1900}, "CR-06"),
    ({}, {"total_exposure": 2900}, "CR-07"),
    ({"leverage": 50}, {}, "CR-08"),
    ({"stop_loss": 52000.0, "contracts": 1, "take_profit": 80000.0}, {}, "CR-09"),
    ({"take_profit": 60300.0}, {}, "CR-10"),
    ({}, {"spread_pct": 0.01}, "CR-11"),
    ({}, {"data_age_s": 60}, "CR-12"),
    ({}, {"data_age_s": float("inf")}, "CR-12"),                # missing data = fail
    ({}, {"last_price": 61000, "mark_price": 60000}, "CR-13"),
    ({}, {"funding_rate": 0.005}, "CR-14"),
    ({"stop_loss": 59400.3}, {}, "CR-16"),
    ({}, {"existing_size": 3, "existing_upnl": -5}, "CR-17"),
    ({}, {"kill_switch": True}, "CR-18"),
    ({}, {"cooldown": True}, "CR-19"),
    ({}, {"orders_last_minute": 99}, "CR-20"),
])
def test_rules_veto(checker, btc, p_kw, c_kw, rule):
    r = checker.check(plan(**p_kw), btc, ctx(**c_kw))
    assert not r.approved and rule in r.veto_ids, r.summary()


def test_short_funding_direction(checker, btc):
    # strongly positive funding: shorts RECEIVE, so a short is allowed
    p = plan(side="sell", stop_loss=60600.0, take_profit=58500.0)
    assert checker.check(p, btc, ctx(funding_rate=0.005)).approved


def test_reduce_only_never_blocked(checker, btc):
    r = checker.check(plan(), btc, ctx(kill_switch=True, data_age_s=float("inf"),
                                       equity=500, reduce_only=True))
    assert r.approved


def test_untradable_instrument(checker, btc):
    from dataclasses import replace
    r = checker.check(plan(), replace(btc, state="expired"), ctx())
    assert "CR-15" in r.veto_ids


def test_kill_switch_file_and_day_roll(tmp_path):
    from datetime import datetime, timezone
    rs = RiskState(tmp_path)
    assert rs.kill_switch_active() == (False, "")
    rs.engage_kill_switch("test")
    assert rs.kill_switch_active() == (True, "test")
    assert RiskState.load(tmp_path).kill_switch_active()[0]   # survives restart
    rs.release_kill_switch()
    assert not rs.kill_switch_active()[0]
    rs.roll_day_if_needed(1000, datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))
    assert rs.day_start_equity == 1000
    assert not rs.roll_day_if_needed(900, datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc))
    assert rs.roll_day_if_needed(900, datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc))
    assert rs.day_start_equity == 900


def test_loss_streak_cooldown(tmp_path):
    rs = RiskState(tmp_path)
    for _ in range(3):
        rs.record_trade_result(-1, cooldown_after=3, cooldown_minutes=10)
    assert rs.in_cooldown() and rs.cooldown_until > time.time()
