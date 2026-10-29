"""
Persistent risk state: UTC-day P&L, loss streaks, order throttle and kill switch.

Crypto runs 24/7, so the "trading day" is the UTC calendar day. The kill switch is
a file (``data/KILL_SWITCH``) so it survives restarts and can be engaged from a
shell (`python -m main kill`) or by `touch data/KILL_SWITCH`.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


@dataclass
class RiskState:
    data_dir: Path
    day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    day_start_equity: float = 0.0
    day_realised_pnl: float = 0.0
    loss_streak: int = 0
    cooldown_until: float = 0.0
    _orders: deque = field(default_factory=deque)

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "risk_state.json"

    # ── kill switch ─────────────────────────────────────────────────────────

    def engage_kill_switch(self, reason: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.kill_file.write_text(json.dumps({
            "reason": reason, "at": datetime.now(timezone.utc).isoformat()}))

    def kill_switch_active(self) -> tuple[bool, str]:
        if self.kill_file.exists():
            try:
                return True, json.loads(self.kill_file.read_text()).get("reason", "")
            except ValueError:
                return True, "manual"
        return False, ""

    def release_kill_switch(self) -> None:
        if self.kill_file.exists():
            self.kill_file.unlink()

    # ── day roll / pnl ──────────────────────────────────────────────────────

    def roll_day_if_needed(self, equity: float, now: datetime | None = None) -> bool:
        today = (now or datetime.now(timezone.utc)).date()
        if today != self.day or self.day_start_equity <= 0:
            self.day = today
            self.day_start_equity = equity
            self.day_realised_pnl = 0.0
            self.save()
            return True
        return False

    def day_pnl(self, equity_now: float) -> float:
        """Equity-based: includes realised, unrealised, fees and funding."""
        return equity_now - self.day_start_equity

    def record_trade_result(self, pnl: float, cooldown_after: int, cooldown_minutes: int) -> None:
        self.day_realised_pnl += pnl
        self.loss_streak = self.loss_streak + 1 if pnl < 0 else 0
        if self.loss_streak >= cooldown_after:
            self.cooldown_until = time.time() + cooldown_minutes * 60
            self.loss_streak = 0
        self.save()

    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    # ── order throttle ──────────────────────────────────────────────────────

    def note_order(self) -> None:
        self._orders.append(time.time())

    def orders_last_minute(self) -> int:
        cutoff = time.time() - 60
        while self._orders and self._orders[0] < cutoff:
            self._orders.popleft()
        return len(self._orders)

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "day": self.day.isoformat(), "day_start_equity": self.day_start_equity,
            "day_realised_pnl": self.day_realised_pnl, "loss_streak": self.loss_streak,
            "cooldown_until": self.cooldown_until}))

    @classmethod
    def load(cls, data_dir: Path) -> RiskState:
        st = cls(data_dir=data_dir)
        if st.state_file.exists():
            try:
                d = json.loads(st.state_file.read_text())
                st.day = date.fromisoformat(d["day"])
                st.day_start_equity = float(d.get("day_start_equity", 0))
                st.day_realised_pnl = float(d.get("day_realised_pnl", 0))
                st.loss_streak = int(d.get("loss_streak", 0))
                st.cooldown_until = float(d.get("cooldown_until", 0))
            except (ValueError, KeyError):
                pass
        return st

    def seconds_to_utc_midnight(self) -> float:
        now = datetime.now(timezone.utc)
        nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
        return (nxt - now).total_seconds()
