"""
Delta Exchange India WebSocket client (asyncio, ``websockets`` library).

* Public channels: ``v2/ticker``, ``l2_orderbook``, ``all_trades``, ``mark_price``,
  ``candlestick_<res>``, ``funding_rate``.
* Private channels (after auth): ``orders``, ``positions``, ``v2/user_trades``, ``margins``.
* Auth message (current method, docs 08.10.25): {"type": "key-auth", "payload":
  {"api-key", "signature", "timestamp"}} with timestamp an integer (seconds) and
  signature = HMAC_SHA256(secret, "GET" + timestamp + "/live"). The old "auth" type
  stopped working after 31-Dec-2025. Success: {"type":"key-auth","success":true}.
* Public market data lives on a separate endpoint with compact channel names
  (``ticker``, ``ob_l2``, ``trades``, ``mark_price``, ``funding_rate``,
  ``candlestick_<res>``); private channels stay on the main endpoint.
* The server disconnects a socket with no activity within 60 s of connecting and
  allows 150 connections per IP per 5 minutes — backoff never goes below 2 s.
* Heartbeat: we send {"type": "enable_heartbeat"}; the server then emits a
  ``heartbeat`` message periodically. Silence longer than ``stale_after_s`` is
  treated as a dead connection and triggers a reconnect.
* Reconnect: exponential backoff with jitter, then re-auth + re-subscribe and an
  ``on_reconnect`` callback so the executor can reconcile state it may have missed.

Verify channel names against https://docs.delta.exchange before going live; they
are configurable through ``subscribe()``.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from infrastructure.core.logger import get_logger
from infrastructure.exchange.rest import sign

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Any]


class DeltaWebSocket:
    def __init__(self, url: str, api_key: str = "", api_secret: str = "",
                 stale_after_s: float = 35.0, max_backoff_s: float = 60.0,
                 min_backoff_s: float = 2.0,
                 connect: Callable[..., Awaitable[Any]] | None = None) -> None:
        self.url = url
        self._key = api_key
        self._secret = api_secret
        self.stale_after_s = stale_after_s
        self.max_backoff_s = max_backoff_s
        self.min_backoff_s = min_backoff_s
        self._connect = connect
        self._subs: dict[str, set[str]] = {}
        self._private: set[str] = set()
        self._handlers: list[Handler] = []
        self._on_reconnect: list[Callable[[], Any]] = []
        self._on_disconnect: list[Callable[[], Any]] = []
        self._ws: Any = None
        self._stop = asyncio.Event()
        self.last_message_at = 0.0
        self.connected = False
        self.authenticated = False
        self.reconnects = 0
        self._ever_connected = False

    # ── registration ────────────────────────────────────────────────────────

    def subscribe(self, channel: str, symbols: list[str] | None = None) -> None:
        self._subs.setdefault(channel, set()).update(symbols or [])

    def subscribe_private(self, channel: str) -> None:
        self._private.add(channel)

    def on_message(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def on_reconnect(self, cb: Callable[[], Any]) -> None:
        self._on_reconnect.append(cb)

    def on_disconnect(self, cb: Callable[[], Any]) -> None:
        self._on_disconnect.append(cb)

    # ── messages ────────────────────────────────────────────────────────────

    def auth_message(self, ts: int | None = None) -> dict[str, Any]:
        ts_i = int(ts or time.time())
        return {"type": "key-auth", "payload": {
            "api-key": self._key, "timestamp": ts_i,
            "signature": sign(self._secret, "GET", str(ts_i), "/live")}}

    def subscribe_message(self) -> dict[str, Any]:
        channels = []
        for name, syms in self._subs.items():
            entry: dict[str, Any] = {"name": name}
            entry["symbols"] = sorted(syms) if syms else ["all"]
            channels.append(entry)
        for name in sorted(self._private):
            channels.append({"name": name, "symbols": ["all"]})
        return {"type": "subscribe", "payload": {"channels": channels}}

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def _open(self) -> Any:
        if self._connect is not None:
            return await self._connect(self.url)
        import websockets
        return await websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                        max_size=2 ** 22, close_timeout=5)

    async def _send(self, msg: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(msg))

    async def _handshake(self) -> None:
        await self._send({"type": "enable_heartbeat"})
        if self._private:
            if not (self._key and self._secret):
                raise RuntimeError("private channels requested but API credentials missing")
            await self._send(self.auth_message())
        if self._subs or self._private:
            await self._send(self.subscribe_message())

    async def run(self) -> None:
        """Connect and pump messages forever, reconnecting on any failure."""
        attempt = 0
        while not self._stop.is_set():
            watchdog: asyncio.Task | None = None
            try:
                self._ws = await self._open()
                self.connected = True
                self.last_message_at = time.monotonic()
                await self._handshake()
                if self._ever_connected:
                    self.reconnects += 1
                    log.info("ws_reconnected", reconnects=self.reconnects)
                    for cb in self._on_reconnect:
                        await _maybe_await(cb())
                self._ever_connected = True
                attempt = 0
                watchdog = asyncio.create_task(self._watchdog())
                async for raw in self._ws:
                    self.last_message_at = time.monotonic()
                    await self._dispatch(raw)
                    if self._stop.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ws_error", error=str(exc)[:300])
            finally:
                if watchdog:
                    watchdog.cancel()
                was_connected = self.connected
                self.connected = False
                self.authenticated = False
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                if was_connected:
                    for cb in self._on_disconnect:
                        await _maybe_await(cb())
            if self._stop.is_set():
                break
            attempt += 1
            delay = max(self.min_backoff_s, min(self.max_backoff_s, (2 ** min(attempt, 6)) * 0.5)
                        * (0.5 + random.random()))
            log.warning("ws_reconnecting", in_s=round(delay, 2), attempt=attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(5.0, self.stale_after_s / 3))
            if time.monotonic() - self.last_message_at > self.stale_after_s:
                log.warning("ws_stale_closing", silent_s=round(time.monotonic() -
                                                               self.last_message_at, 1))
                try:
                    await self._ws.close()
                except Exception:
                    pass
                return

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        mtype = msg.get("type")
        if mtype == "heartbeat":
            return
        if mtype in ("key-auth", "auth", "success"):
            if msg.get("success") is True or msg.get("message") == "Authenticated":
                self.authenticated = True
                log.info("ws_authenticated")
            else:
                self.authenticated = False
                log.critical("ws_auth_failed", status=msg.get("status"),
                             message=msg.get("message"))
            for h in self._handlers:
                await _maybe_await(h(msg))
            return
        if mtype == "subscriptions":
            bad = [c for c in msg.get("channels", []) if c.get("error")]
            if bad:
                log.error("ws_subscription_errors", channels=bad)
            return
        if mtype == "error" or msg.get("success") is False:
            log.error("ws_server_error", msg=msg)
        for h in self._handlers:
            try:
                await _maybe_await(h(msg))
            except Exception as exc:  # a bad handler must not kill the feed
                log.exception("ws_handler_error", error=str(exc), mtype=mtype)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass


async def _maybe_await(x: Any) -> Any:
    if asyncio.iscoroutine(x):
        return await x
    return x
