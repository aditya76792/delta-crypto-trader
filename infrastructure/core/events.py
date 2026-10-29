"""
Synchronous, single-process event bus (preserved from the equity system).

Usage:
    bus = EventBus.get()
    bus.subscribe("CANDLE_CLOSE", handler)
    bus.publish(Event(type="CANDLE_CLOSE", payload=candle))
    bus.process_all()
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Event:
    type: str
    payload: Any
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Crypto event vocabulary (equity-only events such as EOD were removed: markets are 24/7)
    TYPES = frozenset({
        "TICKER",
        "CANDLE_CLOSE",
        "ORDERBOOK",
        "FUNDING",
        "SIGNAL",
        "PLAN_CREATED",
        "PLAN_APPROVED",
        "PLAN_VETOED",
        "ORDER_PLACED",
        "ORDER_UPDATE",
        "ORDER_FILLED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_CANCELLED",
        "ORDER_REJECTED",
        "POSITION_OPENED",
        "POSITION_CLOSED",
        "RECONCILE_MISMATCH",
        "STALE_DATA",
        "WS_DISCONNECTED",
        "WS_RECONNECTED",
        "DAILY_LOSS_LIMIT_HIT",
        "KILL_SWITCH",
        "SYSTEM_ERROR",
    })


class EventBus:
    _instance: EventBus | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Event], None]]] = {}
        self._queue: queue.Queue[Event] = queue.Queue()
        self._processed_count = 0

    @classmethod
    def get(cls) -> EventBus:
        with cls._lock:
            if cls._instance is None:
                cls._instance = EventBus()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[Event], None]) -> bool:
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def publish(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def emit(self, event_type: str, payload: Any = None) -> None:
        self.publish(Event(type=event_type, payload=payload))

    def process_all(self) -> int:
        count = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            for handler in list(self._subscribers.get(event.type, [])):
                handler(event)
            self._processed_count += 1
            count += 1
        return count

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def total_processed(self) -> int:
        return self._processed_count
