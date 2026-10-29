"""
Delta Exchange India REST client.

Auth (official docs): headers ``api-key``, ``timestamp`` (unix seconds), ``signature``
where signature = hex(HMAC_SHA256(secret, method + timestamp + path + query_string + body)).
``query_string`` includes the leading '?'. Signatures older than ~5 s are rejected,
so every retry re-signs. A ``User-Agent`` header is required.

Rate limits: 20,000 weight units per fixed 5-minute window per key; 429 responses
carry ``X-RATE-LIMIT-RESET`` (ms until reset). This client keeps a local weight
budget and honours the reset header.

All methods return the ``result`` member of the ``{"success": true, "result": ...}``
envelope or raise :class:`DeltaAPIError`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlencode

import httpx

from infrastructure.core.logger import get_logger

log = get_logger(__name__)

# Endpoint weights from the Delta rate-limit table
_WEIGHTS = {
    "/v2/products": 3, "/v2/tickers": 3, "/v2/l2orderbook": 3, "/v2/orders": 5,
    "/v2/positions": 3, "/v2/wallet/balances": 3, "/v2/history/candles": 3,
    "/v2/fills": 10, "/v2/orders/history": 10, "/v2/orders/batch": 25,
}
WINDOW_S = 300
WINDOW_QUOTA = 20_000


class DeltaAPIError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None,
                 context: Any = None, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message} (http={status})")
        self.code = code
        self.status = status
        self.context = context
        self.retryable = retryable


class AuthError(DeltaAPIError):
    pass


class RateLimitError(DeltaAPIError):
    pass


# Errors that are never worth retrying blindly (business rejections)
NON_RETRYABLE = {
    "insufficient_margin", "invalid_api_key", "ip_not_whitelisted_for_api_key",
    "UnauthorizedApiAccess", "order_size_exceeded", "immediate_execution_post_only",
    "out_of_bankruptcy", "self_matching_disrupted_post_only", "bad_schema",
    "invalid_contract", "open_order_not_found", "position_size_limit_exceeded",
}


def sign(secret: str, method: str, timestamp: str, path: str,
         query_string: str = "", body: str = "") -> str:
    msg = method.upper() + timestamp + path + query_string + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class _WeightLimiter:
    """Client-side sliding budget so we back off *before* the exchange 429s us."""

    def __init__(self, quota: int = WINDOW_QUOTA, window_s: float = WINDOW_S,
                 safety: float = 0.8) -> None:
        self.quota = int(quota * safety)
        self.window_s = window_s
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self.blocked_until = 0.0

    def acquire(self, weight: int, sleep: Any = time.sleep) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self.blocked_until:
                    wait = self.blocked_until - now
                else:
                    while self._events and now - self._events[0][0] > self.window_s:
                        self._events.popleft()
                    used = sum(w for _, w in self._events)
                    if used + weight <= self.quota:
                        self._events.append((now, weight))
                        return
                    wait = self.window_s - (now - self._events[0][0]) + 0.05
            log.warning("rate_limit_local_wait", wait_s=round(wait, 2))
            sleep(min(wait, 5.0))

    def block_for(self, seconds: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


class DeltaRestClient:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "",
                 user_agent: str = "delta-crypto-trader/0.1", timeout: float = 10.0,
                 max_retries: int = 4, transport: httpx.BaseTransport | None = None,
                 sleep: Any = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._secret = api_secret
        self._ua = user_agent
        self.max_retries = max_retries
        self._sleep = sleep
        self._limiter = _WeightLimiter()
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self.clock_offset_s = 0.0   # server - local, set by sync_time()

    # ── low level ───────────────────────────────────────────────────────────

    def _headers(self, method: str, path: str, qs: str, body: str, auth: bool) -> dict[str, str]:
        h = {"User-Agent": self._ua, "Accept": "application/json",
             "Content-Type": "application/json"}
        if auth:
            if not (self._key and self._secret):
                raise AuthError("missing_credentials",
                                "DELTA_API_KEY / DELTA_API_SECRET not set")
            ts = str(int(time.time() + self.clock_offset_s))
            h.update({"api-key": self._key, "timestamp": ts,
                      "signature": sign(self._secret, method, ts, path, qs, body)})
        return h

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None, auth: bool = False,
                weight: int | None = None, idempotent: bool | None = None) -> Any:
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        qs = ("?" + urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        w = weight or _WEIGHTS.get(path, _WEIGHTS.get("/" + "/".join(path.split("/")[1:3]), 3))
        # POSTs that create orders are only safe to retry because we always send a
        # client_order_id; the caller decides via ``idempotent``.
        if idempotent is None:
            idempotent = method in ("GET", "DELETE")

        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire(w, self._sleep)
            headers = self._headers(method, path, qs, payload, auth)  # re-sign every try
            try:
                resp = self._http.request(method, path + qs, content=payload or None,
                                          headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                err = DeltaAPIError("network_error", str(exc), retryable=True)
                if attempt > self.max_retries or not idempotent:
                    raise err from exc
                self._backoff(attempt, "network_error")
                continue

            if resp.status_code == 429:
                reset_ms = float(resp.headers.get("X-RATE-LIMIT-RESET", "1000") or 1000)
                self._limiter.block_for(reset_ms / 1000.0)
                if attempt > self.max_retries:
                    raise RateLimitError("rate_limited", "429 from exchange", 429, retryable=True)
                log.warning("rate_limited_429", path=path, reset_ms=reset_ms)
                continue

            data = self._decode(resp)
            if resp.status_code < 400 and data.get("success", True):
                return data.get("result", data)

            code, ctx = self._error_code(data, resp)
            retryable = resp.status_code >= 500 or code in ("SignatureExpired", "network_error")
            exc_cls = AuthError if resp.status_code in (401, 403) or code in (
                "invalid_api_key", "ip_not_whitelisted_for_api_key", "UnauthorizedApiAccess",
                "Signature Mismatch") else DeltaAPIError
            err = exc_cls(code, str(ctx or ""), resp.status_code, ctx,
                          retryable and code not in NON_RETRYABLE)
            if err.retryable and (idempotent or code == "SignatureExpired") \
                    and attempt <= self.max_retries:
                if code == "SignatureExpired":
                    self.sync_time()
                self._backoff(attempt, code)
                continue
            log.error("delta_api_error", path=path, method=method, code=code,
                      status=resp.status_code, context=ctx)
            raise err

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(8.0, 0.25 * (2 ** (attempt - 1)))
        log.warning("retrying", reason=why, attempt=attempt, delay_s=delay)
        self._sleep(delay)

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {"success": True, "result": data}
        except ValueError:
            return {"success": resp.status_code < 400, "error": {"code": f"http_{resp.status_code}",
                                                                  "context": resp.text[:200]}}

    @staticmethod
    def _error_code(data: dict[str, Any], resp: httpx.Response) -> tuple[str, Any]:
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("code", f"http_{resp.status_code}")), err.get("context")
        if isinstance(err, str):
            return err, data.get("message")
        return f"http_{resp.status_code}", data.get("message")

    def close(self) -> None:
        self._http.close()

    def sync_time(self) -> None:
        """Estimate server clock offset from the HTTP Date header (best effort)."""
        try:
            r = self._http.get("/v2/products/BTCUSD", headers={"User-Agent": self._ua})
            from email.utils import parsedate_to_datetime
            if "date" in r.headers:
                server = parsedate_to_datetime(r.headers["date"]).timestamp()
                self.clock_offset_s = server - time.time()
        except Exception:  # pragma: no cover - network
            pass

    # ── public market data ──────────────────────────────────────────────────

    def get_products(self, contract_types: str | None = "perpetual_futures,futures",
                     states: str | None = "live") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        while True:
            params = {"contract_types": contract_types, "states": states,
                      "page_size": 500, "after": after}
            res = self.request("GET", "/v2/products", params)
            rows = res if isinstance(res, list) else res.get("result", [])
            out.extend(rows)
            # pagination metadata is outside result; single page is enough in practice
            if len(rows) < 500:
                return out
            after = rows[-1].get("id")

    def get_product(self, symbol: str) -> dict[str, Any]:
        return self.request("GET", f"/v2/products/{symbol}")

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        return self.request("GET", f"/v2/tickers/{symbol}")

    def get_tickers(self, contract_types: str = "perpetual_futures") -> list[dict[str, Any]]:
        return self.request("GET", "/v2/tickers", {"contract_types": contract_types})

    def get_orderbook(self, symbol: str, depth: int = 20) -> dict[str, Any]:
        return self.request("GET", f"/v2/l2orderbook/{symbol}", {"depth": depth})

    def get_trades(self, symbol: str) -> Any:
        return self.request("GET", f"/v2/trades/{symbol}")

    def get_candles(self, symbol: str, resolution: str, start: int, end: int) -> list[dict]:
        """Up to 2000 candles per call; start/end in unix seconds."""
        return self.request("GET", "/v2/history/candles",
                            {"resolution": resolution, "symbol": symbol,
                             "start": start, "end": end}) or []

    # ── private: account ────────────────────────────────────────────────────

    def get_balances(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v2/wallet/balances", auth=True)

    def get_positions(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v2/positions/margined", auth=True)

    def get_position(self, product_id: int) -> dict[str, Any]:
        return self.request("GET", "/v2/positions", {"product_id": product_id}, auth=True)

    def get_open_orders(self, product_id: int | None = None) -> list[dict[str, Any]]:
        return self.request("GET", "/v2/orders",
                            {"product_ids": product_id, "states": "open,pending"}, auth=True)

    def get_fills(self, product_id: int | None = None, page_size: int = 50) -> list[dict]:
        return self.request("GET", "/v2/fills",
                            {"product_ids": product_id, "page_size": page_size}, auth=True)

    def set_leverage(self, product_id: int, leverage: float) -> dict[str, Any]:
        return self.request("POST", f"/v2/products/{product_id}/orders/leverage",
                            body={"leverage": str(leverage)}, auth=True, idempotent=True)

    # ── private: orders ─────────────────────────────────────────────────────

    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        # Safe to retry ONLY because body carries a unique client_order_id
        # and the executor checks for an existing order before re-sending.
        return self.request("POST", "/v2/orders", body=body, auth=True,
                            idempotent=bool(body.get("client_order_id")))

    def edit_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", "/v2/orders", body=body, auth=True, idempotent=True)

    def cancel_order(self, product_id: int, order_id: int | None = None,
                     client_order_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"product_id": product_id}
        if order_id is not None:
            body["id"] = order_id
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self.request("DELETE", "/v2/orders", body=body, auth=True)

    def cancel_all(self, product_id: int | None = None) -> Any:
        body = {"product_id": product_id} if product_id else {
            "contract_types": "perpetual_futures,futures"}
        body.update({"cancel_limit_orders": True, "cancel_stop_orders": True})
        return self.request("DELETE", "/v2/orders/all", body=body, auth=True)

    def place_bracket(self, body: dict[str, Any]) -> Any:
        return self.request("POST", "/v2/orders/bracket", body=body, auth=True, idempotent=True)
